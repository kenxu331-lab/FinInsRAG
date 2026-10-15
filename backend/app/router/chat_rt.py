from fastapi import APIRouter, Body, UploadFile, File, HTTPException, Query, Security, status, Depends
import uuid
import time
from schemas.chat import SessionResponse, ChatRequest
from fastapi.responses import StreamingResponse
import os
from dotenv import load_dotenv
from typing import List, Optional
from service.core.api.utils.file_utils import get_project_base_directory
from fastapi_jwt import JwtAuthorizationCredentials
from service.core.retrieval import RetrievalUnavailable, retrieve_content
from service.core.chat import (
    get_chat_completion,
    load_conversation_history,
    rewrite_followup_question,
)
from service.auth import access_security
from utils import logger
from sqlalchemy.orm import Session
from utils.database import get_db
from service.core.ingestion import get_session_document_content, ingest_document
from service.document_upload_service import DocumentUploadService
from schemas.document_upload import DocumentUploadResponse, SessionDocumentsResponse, SessionDocumentSummary

# 加载 .env 文件
load_dotenv()

# 配置日志
logger.info(f"ES_HOST: {os.getenv('ES_HOST')}")
logger.info(f"ELASTICSEARCH_URL: {os.getenv('ELASTICSEARCH_URL')}")

router = APIRouter()

##################################
# 创建一个新的对话 Session
##################################

@router.post("/create_session", response_model=SessionResponse)
async def create_session(
    credentials: JwtAuthorizationCredentials = Security(access_security),
):
    try:
        print(credentials)
        user_id = credentials.subject.get("user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid authentication credentials")

        session_id = str(uuid.uuid4()).replace("-", "")[:16]

        return {
            "session_id": session_id,
            "status": "success",
            "message": "Session created successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

##################################
# 快速文档解析接口
##################################

@router.post("/quick_parse")
async def quick_parse_document(
    session_id: str = Query(..., description="会话ID"),
    file: UploadFile = File(..., description="要解析的文档"),
    credentials: JwtAuthorizationCredentials = Security(access_security),
    db: Session = Depends(get_db),
):
    """
    快速文档解析接口
    - 支持 docx、pdf、txt、md 和 Excel
    - 与持久文档共用解析、切分、Embedding 和索引流程
    - 按 user_id + session_id 隔离，默认保存 24 小时
    """
    try:
        user_id = str(credentials.subject.get("user_id"))
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid authentication credentials")

        # 读取文件内容
        file_content = await file.read()

        # 获取文件信息
        file_size = len(file_content)
        file_extension = os.path.splitext(file.filename)[1].lower() if file.filename else ""
        document_type = file_extension.replace(".", "") if file_extension else "unknown"

        if not file_content:
            raise HTTPException(status_code=400, detail="文件内容为空")
        safe_name = os.path.basename(file.filename or "document")
        allowed = {".pdf", ".docx", ".txt", ".md", ".xlsx", ".xls"}
        if file_extension not in allowed:
            raise HTTPException(status_code=400, detail=f"不支持的文件格式: {file_extension}")
        storage_dir = os.path.join(get_project_base_directory(), "storage", "file", session_id)
        os.makedirs(storage_dir, exist_ok=True)
        stored_name = f"quick_{uuid.uuid4().hex}_{safe_name}"
        file_path = os.path.join(storage_dir, stored_name)
        with open(file_path, "wb") as buffer:
            buffer.write(file_content)
        result = ingest_document(
            file_path=file_path,
            file_name=safe_name,
            user_id=user_id,
            session_id=session_id,
            scope="session",
            mime_type=file.content_type,
            file_size=file_size,
            db=db,
        )
        result.update(
            {
                "status": "success",
                "message": "文档解析并索引完成",
                "session_id": session_id,
                "filename": safe_name,
                "file_type": document_type,
                "content_length": file_size,
                "expiry_hours": 24,
            }
        )

        # 记录文档上传信息到数据库
        try:
            DocumentUploadService.create_upload_record(
                db=db,
                session_id=session_id,
                document_name=file.filename,
                document_type=document_type,
                file_size=file_size
            )
            logger.info(f"文档上传记录已保存: session_id={session_id}, document_name={file.filename}")
        except Exception as db_error:
            logger.error(f"保存文档上传记录失败: {str(db_error)}")
            # 数据库记录失败不影响主要功能，继续返回解析结果

        logger.info(f"用户 {user_id} 的文档解析完成，session_id: {session_id}")
        return result

    except HTTPException as e:
        logger.error(f"快速解析错误: {str(e)}")
        raise e
    except Exception as e:
        logger.exception(f"快速解析发生未知错误: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"内部服务器错误: {str(e)}"
        )

##################################
# 获取解析内容接口
##################################

@router.get("/get_parsed_content")
async def get_parsed_content(
    session_id: str = Query(..., description="会话ID"),
    credentials: JwtAuthorizationCredentials = Security(access_security),
):
    """
    获取已解析的文档内容
    """
    try:
        user_id = str(credentials.subject.get("user_id"))
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid authentication credentials")

        try:
            result = get_session_document_content(user_id, session_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        logger.info(f"用户 {user_id} 获取解析内容，session_id: {session_id}")
        return result

    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )

##################################
# 基于统一证据索引的文档对话
##################################

@router.post("/chat_on_docs")
async def chat_on_docs(
    session_id: str = Query(...),
    request: ChatRequest = Body(..., description="User message"),
    credentials: JwtAuthorizationCredentials = Security(access_security),
):
    request_id = uuid.uuid4().hex
    try:
        user_id = str(credentials.subject.get("user_id"))
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid authentication credentials")

        logger.info("request_id=%s user_id=%s question_length=%s", request_id, user_id, len(request.message))

        question = request.message
        history = load_conversation_history(session_id, user_id)
        retrieval_query = rewrite_followup_question(question, history)
        logger.info(
            "request_id=%s history_turns=%s query_rewritten=%s",
            request_id,
            len(history),
            retrieval_query != question,
        )

        # 无候选时生成层统一拒答；双路检索不可用时返回 503。
        references = []
        try:
            retrieval_started = time.perf_counter()
            references = retrieve_content(user_id, retrieval_query, session_id)
            logger.info(
                "request_id=%s retrieval_ms=%.1f evidence_count=%s",
                request_id,
                (time.perf_counter() - retrieval_started) * 1000,
                len(references),
            )
        except RetrievalUnavailable as e:
            logger.error("Retrieval unavailable for user_id=%s: %s", user_id, e)
            raise HTTPException(status_code=503, detail="Document retrieval is temporarily unavailable") from e

        logger.info("开始生成回答...")
        # 返回流式响应
        return StreamingResponse(
            get_chat_completion(
                session_id,
                question,
                references,
                user_id,
                request_id,
                history,
            ),
            media_type="text/event-stream",
            headers={"X-Request-ID": request_id},
        )

    except HTTPException as e:
        logger.error(f"HTTP错误: {str(e)}")
        raise e
    except Exception as e:
        logger.exception(f"发生未知错误: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )

@router.post("/upload_files")
async def upload_files(
    session_id: Optional[str] = Query(None),
    files: List[UploadFile] = File(...),
    credentials: JwtAuthorizationCredentials = Security(access_security),
    db: Session = Depends(get_db)
):
    try:
        user_id = str(credentials.subject.get("user_id"))
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid authentication credentials")

        # 如果没有 session_id，使用 user_id 作为 session_id
        if session_id is None:
            session_id = user_id

        # 确保 storage/file 文件夹存在
        storage_dir = os.path.join(get_project_base_directory(), "storage/file")
        if not os.path.exists(storage_dir):
            os.makedirs(storage_dir)

        # 根据 session_id 创建子文件夹
        session_dir = os.path.join(storage_dir, session_id)
        if not os.path.exists(session_dir):
            os.makedirs(session_dir)

        # 处理文件上传
        successful_files = []
        failed_files = []
        indexed_documents = []

        for file in files:
            file_name = os.path.basename(file.filename or "document")
            file_path = os.path.join(session_dir, f"{uuid.uuid4().hex}_{file_name}")

            try:
                # 读取文件内容
                file_content = await file.read()

                # 验证文件内容不为空
                if not file_content:
                    failed_files.append(f"{file_name}: 文件内容为空")
                    continue

                # 对于 Excel 文件，进行额外验证
                if file_name.lower().endswith(('.xlsx', '.xls')):
                    # 检查文件头，xlsx 文件应该是 ZIP 格式
                    if file_name.lower().endswith('.xlsx'):
                        # XLSX 文件应该以 PK 开头（ZIP 文件头）
                        if not file_content.startswith(b'PK'):
                            failed_files.append(f"{file_name}: 不是有效的 XLSX 文件格式，可能是 XLS 文件")
                            continue
                    elif file_name.lower().endswith('.xls'):
                        # XLS 文件有特定的文件头
                        if not (file_content.startswith(b'\xd0\xcf\x11\xe0') or
                               file_content.startswith(b'\x09\x08')):
                            failed_files.append(f"{file_name}: 不是有效的 XLS 文件格式")
                            continue

                # 保存文件到本地
                with open(file_path, "wb") as buffer:
                    buffer.write(file_content)

                # 验证文件大小
                if os.path.getsize(file_path) != len(file_content):
                    failed_files.append(f"{file_name}: 文件保存失败，大小不匹配")
                    continue

                # 保存文件 URL 和 Base64 编码的文件流
                file_url = file_path
                logger.info(f"Processing file: {file_url}")

                # 尝试解析和插入文档
                try:
                    ingest_result = ingest_document(
                        file_path=file_url,
                        file_name=file_name,
                        user_id=user_id,
                        scope="user",
                        mime_type=file.content_type,
                        file_size=len(file_content),
                        db=db,
                    )
                    successful_files.append(file_name)
                    indexed_documents.append({"file_name": file_name, **ingest_result})

                except Exception as parse_error:
                    logger.error(f"文件解析失败 {file_name}: {str(parse_error)}")
                    failed_files.append(f"{file_name}: 文件解析失败 - {str(parse_error)}")
                    # 删除已保存的文件
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    continue

            except Exception as e:
                logger.error(f"处理文件失败 {file_name}: {str(e)}")
                failed_files.append(f"{file_name}: 处理失败 - {str(e)}")
                continue

        # 构建返回结果
        if successful_files and not failed_files:
            return {
                "status": "success",
                "message": "所有文件解析成功",
                "successful_files": successful_files,
                "documents": indexed_documents,
                "total_files": len(files)
            }
        elif successful_files and failed_files:
            return {
                "status": "partial_success",
                "message": f"部分文件解析成功，{len(successful_files)} 个成功，{len(failed_files)} 个失败",
                "successful_files": successful_files,
                "documents": indexed_documents,
                "failed_files": failed_files,
                "total_files": len(files)
            }
        else:
            raise HTTPException(
                status_code=400,
                detail={
                    "status": "failed",
                    "message": "所有文件解析失败",
                    "failed_files": failed_files,
                    "total_files": len(files)
                }
            )

    except HTTPException as e:
        raise e
    except Exception as e:
        logger.exception(e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )

##################################
# 查询会话文档上传信息接口
##################################

@router.get("/sessions/{session_id}/documents", response_model=SessionDocumentsResponse)
async def get_session_documents(
    session_id: str,
    credentials: JwtAuthorizationCredentials = Security(access_security),
    db: Session = Depends(get_db),
):
    """
    获取指定会话的所有文档上传记录
    """
    try:
        user_id = str(credentials.subject.get("user_id"))
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid authentication credentials")

        # 获取会话的所有文档记录
        documents = DocumentUploadService.get_session_documents(db, session_id)
        has_documents = len(documents) > 0

        return SessionDocumentsResponse(
            session_id=session_id,
            has_documents=has_documents,
            documents=[DocumentUploadResponse.from_orm(doc) for doc in documents],
            total_count=len(documents)
        )

    except HTTPException as e:
        raise e
    except Exception as e:
        logger.exception(f"获取会话文档信息失败: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@router.get("/sessions/{session_id}/documents/summary", response_model=SessionDocumentSummary)
async def get_session_document_summary(
    session_id: str,
    credentials: JwtAuthorizationCredentials = Security(access_security),
    db: Session = Depends(get_db),
):
    """
    获取指定会话的文档上传摘要信息
    """
    try:
        user_id = str(credentials.subject.get("user_id"))
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid authentication credentials")

        # 检查是否有上传的文档
        has_documents = DocumentUploadService.has_uploaded_documents(db, session_id)

        # 获取最新的文档信息
        latest_document = DocumentUploadService.get_latest_document(db, session_id)

        # 获取总文档数量
        all_documents = DocumentUploadService.get_session_documents(db, session_id)
        total_documents = len(all_documents)

        return SessionDocumentSummary(
            session_id=session_id,
            has_documents=has_documents,
            latest_document_name=latest_document.document_name if latest_document else None,
            latest_document_type=latest_document.document_type if latest_document else None,
            latest_upload_time=latest_document.upload_time if latest_document else None,
            total_documents=total_documents
        )

    except HTTPException as e:
        raise e
    except Exception as e:
        logger.exception(f"获取会话文档摘要失败: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )

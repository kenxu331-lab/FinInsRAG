from fastapi import APIRouter, Depends, HTTPException, Query, Security
from sqlalchemy.orm import Session
from utils.database import get_db
from models.document import Document
from schemas.message import FilestResponse , SessionListResponse, SessionResponse
from fastapi_jwt import JwtAuthorizationCredentials
from service.auth import access_security
from typing import List
from sqlalchemy import text ,select 
from urllib.parse import unquote
from service.document_operations import delete_document

router = APIRouter()

############################
#   获取文档列表
############################

@router.get("/get_files", response_model=List[FilestResponse])
async def get_documents_by_user_id(
    credentials: JwtAuthorizationCredentials = Security(access_security),
    db: Session = Depends(get_db)
):
    """
    获取用户上传的文档列表，需要用户认证
    """
    try:
        # 从 token 中获取用户 ID
        user_id = str(credentials.subject.get("user_id"))
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid authentication credentials")

        # 构建查询语句
        stmt = select(Document).where(
            Document.user_id == user_id,
            Document.scope == "user",
            Document.parse_status == "ready",
        )
        
        # 执行查询
        result = db.execute(stmt).scalars().all()

        # 如果没有找到文档，返回空列表
        if not result:
            return []

        # 将查询结果转换为 Pydantic 模型
        documents = [
            FilestResponse(
                user_id=row.user_id,
                file_name=row.file_name,
                chunk_count=row.chunk_count,
                created_at=row.created_at.isoformat(),
                updated_at=row.updated_at.isoformat()
            )
            for row in result
        ]

        return documents

    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve documents: {str(e)}"
        )

############################
#   删除文档
############################

@router.delete("/delete_file/{file_name}")
async def delete_document_endpoint(
    file_name: str,
    credentials: JwtAuthorizationCredentials = Security(access_security),
    db: Session = Depends(get_db)
):
    try:
        # URL 解码文件名
        decoded_file_name = unquote(file_name)
        
        user_id = str(credentials.subject.get("user_id"))
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid authentication credentials")

        # 调用 service 层的删除方法
        result = delete_document(user_id, decoded_file_name, db)
        
        if result["status"] == "error":
            raise HTTPException(status_code=404, detail=result["message"])
            
        return {"message": result["message"]}

    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/get_messages")
async def get_messages_by_session_id(
    session_id: str,
    credentials: JwtAuthorizationCredentials = Security(access_security),
    db: Session = Depends(get_db)
):
    try:
        user_id = str(credentials.subject.get("user_id"))
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid authentication credentials")

        # 会话历史必须同时按 session_id 和当前 user_id 隔离。
        messages_data = db.execute(
            text(
                """
                SELECT m.message_id, m.session_id, m.user_question, m.model_answer,
                       m.documents, m.recommended_questions, m.think, m.created_at
                FROM messages AS m
                JOIN sessions AS s ON s.session_id = m.session_id
                WHERE m.session_id = :session_id AND s.user_id = :user_id
                ORDER BY m.created_at ASC, m.message_id ASC
                """
            ),
            {"session_id": session_id, "user_id": user_id}
        ).fetchall()

        # 构造返回数据
        messages = []
        for message in messages_data:
            messages.append(
                {
                    "message_id": message.message_id,
                    "session_id": message.session_id,
                    "user_question": message.user_question,
                    "model_answer":message.model_answer,
                    "documents" : message.documents,
                    "recommended_questions" : message.recommended_questions,
                    "think" : message.think,
                    "created_at": message.created_at.strftime("%Y-%m-%d %H:%M:%S")
                }
            )

        return messages

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve messages: {str(e)}"
        )
    
@router.get("/get_sessions", response_model=SessionListResponse)
async def get_sessions_by_user_id(
    credentials: JwtAuthorizationCredentials = Security(access_security),
    db: Session = Depends(get_db)
):
    try:
        user_id = str(credentials.subject.get("user_id"))
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid authentication credentials")


        # 查询 sessions 表中对应 user_id 的所有会话
        sessions_data = db.execute(
            text("SELECT * FROM sessions WHERE user_id = :user_id"),
            {"user_id": user_id}
        ).fetchall()

        # 构造返回数据
        sessions = []
        for session in sessions_data:
            sessions.append(
                SessionResponse(
                    session_id=session.session_id,
                    session_name=session.session_name,
                    user_id=session.user_id,
                    created_at=session.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                    updated_at=session.updated_at.strftime("%Y-%m-%d %H:%M:%S")
                )
            )

        return {"user_id": user_id, "sessions": sessions}

    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

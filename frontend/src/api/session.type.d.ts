declare namespace API {
  interface Session {
    created_at: string
    session_id: string
    session_name: string
    updated_at: string
    // user_id: string
  }

  interface ChatItem {
    id: number
    role: import('@/configs').ChatRole
    type: import('@/configs').ChatType
    loading?: boolean
    error?: string
    content?: string
    think?: string

    documents?: Document[]
    reference?: Reference[]
    recommended_questions?: string[]
  }

  interface Document {
    document_id: string
    document_name: string
    content_with_weight: string
  }

  interface Reference {
    id: string
    citation_id?: string
    chunk_id?: string
    document_id: string
    document_name: string
    content_with_weight: string
    positions: number[][]
    page?: number
    heading_path?: string[]
    block_type?: string
    retrieval_sources?: Array<'bm25' | 'dense' | string>
  }
}

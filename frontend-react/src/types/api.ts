export type Provider = 'local' | 'openai' | 'gemini' | 'anthropic' | 'ollama';
export type BBox = [number, number, number, number];
export interface Session {user: string; csrf_token: string}
export interface Setup {registration_required?: boolean; local_registration_available?: boolean}
export interface SystemStatus {
  llm_provider: Provider; llm_model: string; vector_backend: string; total_chunks: number;
  circuit_breaker_state: string; embedding_model: string; environment?: string; app_version?: string;
  build_id?: string; credentials_editable?: boolean; provider_ready?: boolean; supports_images?: boolean;
  semantic_retrieval?: boolean; generation_mode?: string; migration_status?: string;
  index_compatible?: boolean; openai_base_url?: string; ollama_base_url?: string;
  response_format?: string; openai_backend?: string;
}
export interface DocumentRecord {
  document_id: string; file_name: string; page_count: number; chunk_count: number;
  visual_count: number; table_count: number; sha256: string; status: string;
}
export interface Job {
  job_id: string; file_name: string; document_id?: string; status: string;
  progress: number; message?: string; error?: string;
}
export interface ConversationTurn {role: 'user' | 'assistant'; content: string}
export interface Citation {
  ref: number; chunk_id: string; document_id: string; document_name: string;
  page: number; section: string; kind: string; snippet: string; bbox: BBox;
  score: number; dense_score?: number | null; sparse_score?: number | null;
  page_image_url?: string; modality?: string;
}
export interface VisualAsset {
  asset_id: string; document_id: string; document_name: string; page: number;
  caption?: string; image_url?: string; page_image_url?: string; bbox: BBox;
  anchor_bbox?: BBox; anchor_chunk_id?: string; distance?: number | null; reason?: string;
}
export interface ChatResponse {
  answer: string; answer_mode: string; abstained: boolean; evidence_coverage: string;
  model: string; latency_ms: number; timings_ms: Record<string, number>;
  citations: Citation[]; primary_visual?: VisualAsset | null; related_visuals?: VisualAsset[];
  metrics?: {max_cosine_similarity?: number | null; lexical_score?: number | null; top_rrf_score?: number | null};
  metadata?: {
    accepted_claims?: number; rejected_claims?: number; sent_asset_ids?: string[]; used_asset_ids?: string[];
    provider_reason?: string; retrieved_sources?: {document: string; page: number; snippet: string}[];
    checks?: string; embedding_profile_id?: string; generation_id?: string;
  };
}
export interface ChatMessage {
  id: string; kind: 'user' | 'pending' | 'answer' | 'error' | 'cancelled'; text: string;
  response?: ChatResponse; started?: number; question?: string;
}
export type ProviderPayload = {llm_provider: Provider} & Record<string, string | boolean>;

export type View = "new" | "tasks" | "settings" | "components";
export type Progress = {
  completed_ms?: number;
  total_ms?: number;
  completed_bytes?: number;
  total_bytes?: number;
  percent?: number;
};
export type Artifact = {
  artifact_id: string;
  kind: string;
  format: string;
  content_url: string;
  size_bytes: number;
};
export type Segment = {
  segment_id: string;
  start_ms: number;
  end_ms: number;
  text: string;
  needs_review: boolean;
};
export type Transcript = {
  version_id: string;
  language: string;
  duration_ms: number;
  segments?: Segment[];
  warnings: string[];
};
export type Claim = { text: string; evidence_ids: string[] };
export type Evidence = {
  evidence_id: string;
  start_ms: number;
  end_ms: number;
  quotes: string[];
};
export type Output = {
  id: string;
  type: string;
  status: string;
  claims?: Claim[];
  error?: { message: string };
};
export type Result = {
  transcript?: Transcript;
  evidence?: Evidence[];
  outputs?: Output[];
  artifacts?: Artifact[];
  warnings?: string[];
};
export type Job = {
  job_id: string;
  asset_id: string | null;
  kind: string;
  status: string;
  attempt: number;
  current_stage: string;
  progress: Progress;
  created_at: string;
  finished_at?: string;
  error?: { message: string; code: string };
  warnings: string[];
  artifacts?: Artifact[];
  stages?: { name: string; status: string }[];
};
export type Model = { id: string; state: string };
export type XhsStatus = {
  configured: boolean;
  local_available: boolean;
};
export type Provider = {
  id: string;
  name: string;
  protocol: "openai_compatible";
  base_url: string;
  credential_ref: string | null;
  timeout_seconds: number;
};
export type Binding = {
  provider_id: string;
  model: string;
  capabilities: ("text" | "vision")[];
};
export type Profile = {
  id: string;
  model_id: string;
  language: string;
  device: "cpu" | "cuda";
  compute_type: string;
  glossary: string;
  text_binding_id: string | null;
  vision_binding_id: string | null;
  allow_remote_ai: boolean;
};
export type Settings = {
  revision: number;
  providers: Provider[];
  model_bindings: Record<string, Binding>;
  processing_profiles: Profile[];
  max_duration_seconds: number;
  max_upload_mb: number;
  queue_limit: number;
  cpu_threads: number;
  adapters: Record<string, boolean>;
  source_credentials: Record<string, string>;
  source_resolver: {
    enabled: boolean;
    download_session_bound: boolean;
    base_url: string;
    credential_ref: string | null;
    timeout_seconds: number;
  };
  xhs_resolver: {
    mode: "local" | "remote";
    enabled: boolean;
    base_url: string;
    credential_ref: string | null;
    timeout_seconds: number;
  };
};

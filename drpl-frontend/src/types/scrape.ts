export interface ScrapeSession {
  id: number;
  user_id: number;
  portal: string;
  session_id: string | null;
  status: string | null;
  tenders_found: number;
  error_message: string | null;
  started_at: string;
  completed_at: string | null;
}

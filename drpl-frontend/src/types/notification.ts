export type NotificationKind =
  | 'tender.assigned'
  | 'tender.analysis_complete'
  | 'tender.costing_complete'
  | 'tender.annexures_extracted'
  | 'tender.closing_soon'
  | 'proposal.submitted'
  | 'proposal.approved'
  | 'proposal.rejected'
  | 'proposal.changes_requested'
  | 'agent_run.failed'
  | 'digest.daily'
  | string; // permissive — admin may seed new kinds later

export interface AppNotification {
  id: number;
  kind: NotificationKind;
  title: string;
  body: string | null;
  action_url: string | null;
  tender_id: number | null;
  is_read: boolean;
  read_at: string | null;
  email_sent_at: string | null;
  created_at: string;
}

export interface NotificationListResponse {
  total: number;
  limit: number;
  offset: number;
  items: AppNotification[];
}

export interface NotificationPreference {
  kind: string;
  in_app_enabled: boolean;
  email_enabled: boolean;
  description: string | null;
  updated_at: string | null;
}

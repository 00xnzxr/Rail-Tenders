export interface PortalHealthInfo {
  portal: string;
  status: 'healthy' | 'warning' | 'error';
  last_successful_scrape: string | null;
  tenders_24h: number;
  tenders_7d: number;
  tenders_30d: number;
  success_rate: number;
  last_error: string | null;
  selectors_version: string;
}

export interface PortalAlert {
  portal: string;
  alert_type: string;
  message: string;
  severity: 'warning' | 'critical';
  detected_at: string;
}

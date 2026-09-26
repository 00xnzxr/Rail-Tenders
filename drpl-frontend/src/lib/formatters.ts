import { format, formatDistanceToNow, parseISO } from 'date-fns';

export function formatCurrency(value: number | null | undefined, currency = 'INR'): string {
  if (value == null) return '-';
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency,
    maximumFractionDigits: 0,
  }).format(value);
}

export function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return '-';
  try {
    return format(parseISO(dateStr), 'dd MMM yyyy');
  } catch {
    return '-';
  }
}

export function formatDateTime(dateStr: string | null | undefined): string {
  if (!dateStr) return '-';
  try {
    return format(parseISO(dateStr), 'dd MMM yyyy, HH:mm');
  } catch {
    return '-';
  }
}

export function formatTimeAgo(dateStr: string | null | undefined): string {
  if (!dateStr) return '-';
  try {
    return formatDistanceToNow(parseISO(dateStr), { addSuffix: true });
  } catch {
    return '-';
  }
}

export function portalLabel(portal: string): string {
  const labels: Record<string, string> = {
    ireps: 'IREPS',
    gem: 'GeM',
    tendertiger: 'TenderTiger',
    bidassist: 'BidAssist',
    cppp: 'CPPP',
    eproc: 'eProcurement',
  };
  return labels[portal] || portal;
}

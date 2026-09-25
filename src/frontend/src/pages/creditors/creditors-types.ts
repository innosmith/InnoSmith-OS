import type React from 'react';

export interface DashboardData {
  kpis: Record<string, unknown>;
  cost_distribution: Record<string, unknown>;
  metadata: Record<string, unknown>;
}

export interface RenewalEntry {
  vendor: string;
  product?: string;
  next_date: string;
  days_until: number;
  amount_chf?: number;
  cycle?: string;
  currency?: string;
  [key: string]: unknown;
}

export interface RenewalCalendar {
  critical?: RenewalEntry[];
  warning?: RenewalEntry[];
  info?: RenewalEntry[];
  stable?: RenewalEntry[];
  [key: string]: unknown;
}

export interface AnomalyEntry {
  vendor: string;
  old_amount?: number;
  new_amount?: number;
  change_pct?: number;
  severity: string;
  detail?: string;
  [key: string]: unknown;
}

export interface AnomalyData {
  critical?: AnomalyEntry[];
  warning?: AnomalyEntry[];
  info?: AnomalyEntry[];
  stable?: AnomalyEntry[];
  [key: string]: unknown;
}

export interface VendorRow {
  vendor: string;
  total_chf: number;
  invoice_count: number;
  avg_chf?: number;
  share_pct?: number;
  category?: string;
  [key: string]: unknown;
}

export interface InvoiceRow {
  index?: number;
  invoice_id?: number;
  /** Datenbankkennung des Belegs. Nicht dasselbe wie `index` oder `invoice_id`:
   *  die beiden sind Zeilennummern im Abzug und verschieben sich bei jedem
   *  Filter. Nur diese Kennung taugt zum Schreiben. */
  beleg_id?: number;
  vendor?: string;
  date?: string;
  amount?: number;
  amount_chf?: number;
  currency?: string;
  category?: string;
  product?: string;
  filename?: string;
  [key: string]: unknown;
}

export interface CreditorsFilter {
  yearFrom?: number;
  yearTo?: number;
  categories?: string[];
  vendors?: string[];
}

export type CreditorsTab =
  | 'eingang'
  | 'uebersicht'
  | 'rechnungen'
  | 'erneuerungen'
  | 'anbieter'
  | 'trends'
  | 'anomalien'
  | 'research';

export interface TabDef {
  id: CreditorsTab;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
}

export interface StyleCtx {
  hasBg: boolean;
  cardClass: string;
  sectionClass: string;
  textPrimary: string;
  textSecondary: string;
  textMuted: string;
}

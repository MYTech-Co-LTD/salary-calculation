import axios from "axios";

export const http = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || "/api",
});

const TOKEN_KEY = "salary_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(t: string) {
  localStorage.setItem(TOKEN_KEY, t);
}
export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

// 请求拦截器：带 token
http.interceptors.request.use((config) => {
  const t = getToken();
  if (t) config.headers.Authorization = `Bearer ${t}`;
  return config;
});

// 响应拦截器：401 清 token
http.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err.response?.status === 401) clearToken();
    return Promise.reject(err);
  },
);

// —— 端点封装 ——
export const authApi = {
  login: (username: string, password: string) =>
    http.post<{ token: string }>("/auth/login", { username, password }).then((r) => r.data),
  me: () => http.get<{ username: string }>("/auth/me").then((r) => r.data),
};

export interface Product {
  barcode: string;
  name?: string;
  spec?: string;
  category?: string;
  cost?: number | null;
  exclude_commission?: boolean;
}

export const productsApi = {
  list: () => http.get<Product[]>("/products").then((r) => r.data),
  upsert: (p: Product) => http.put<Product>(`/products/${p.barcode}`, p).then((r) => r.data),
  patch: (barcode: string, data: Partial<Product>) => http.patch<Product>(`/products/${barcode}`, data).then((r) => r.data),
  remove: (barcode: string) => http.delete(`/products/${barcode}`).then((r) => r.data),
};

export interface Store {
  name: string;
  group?: string;
  store_class?: string;
  supervisor?: string;
  exclude_assessment?: boolean;
}

export const storesApi = {
  list: () => http.get<Store[]>("/stores").then((r) => r.data),
  upsert: (s: Store) => http.put<Store>(`/stores/${s.name}`, s).then((r) => r.data),
  patch: (name: string, data: Partial<Store>) => http.patch<Store>(`/stores/${name}`, data).then((r) => r.data),
  remove: (name: string) => http.delete(`/stores/${name}`).then((r) => r.data),
  batchClass: (group: string, store_class: string) =>
    http.post<{ updated: number }>("/stores/batch-class", { group, store_class }).then((r) => r.data),
};

// —— 月度与工作流 ——
export interface MonthInfo {
  month: string;
  status?: string;
  sales_file?: string | null;
  gifts_file?: string | null;
  current_step?: string;
  step_data?: Record<string, boolean>;
}

export const monthsApi = {
  list: () => http.get<MonthInfo[]>("/months").then((r) => r.data),
  create: (month: string, copy_from?: string) =>
    http.post<MonthInfo>("/months", { month, copy_from }).then((r) => r.data),
  get: (month: string) => http.get<MonthInfo>(`/months/${month}`).then((r) => r.data),
};

export interface Target {
  id: number;
  month: string;
  store: string;
  target: number;
}

export const targetsApi = {
  list: (month?: string) => http.get<Target[]>("/targets", { params: month ? { month } : {} }).then((r) => r.data),
  get: (month: string) => http.get<Record<string, Record<string, number>>>(`/months/${month}/targets`).then((r) => r.data),
  set: (month: string, items: { store: string; target: string }[]) =>
    http.put(`/months/${month}/targets`, { items }).then((r) => r.data),
  create: (data: { month: string; store: string; target: number }) =>
    http.post<Target>("/targets", data).then((r) => r.data),
  batchCreate: (month: string) =>
    http.post<{ created: number; stores: string[] }>(`/targets/batch?month=${month}`).then((r) => r.data),
  batchSet: (month: string, items: { store: string; target: string }[]) =>
    http.put(`/months/${month}/targets`, { items }).then((r) => r.data),
  update: (id: number, target: number) =>
    http.put<Target>(`/targets/${id}?target_value=${target}`).then((r) => r.data),
  delete: (id: number) => http.delete(`/targets/${id}`).then((r) => r.data),
  deleteMonth: (month: string) => http.delete(`/targets/month/${month}`).then((r) => r.data),
};

export type DutyGrid = Record<string, Record<string, string | string[]>>;

export const workflowApi = {
  importSales: (month: string, file: File) => {
    const fd = new FormData(); fd.append("file", file);
    return http.post(`/months/${month}/import-sales`, fd).then((r) => r.data);
  },
  importGifts: (month: string, file: File) => {
    const fd = new FormData(); fd.append("file", file);
    return http.post(`/months/${month}/import-gifts`, fd).then((r) => r.data);
  },
  inferDuty: (month: string) => http.post<DutyGrid>(`/months/${month}/infer-duty`).then((r) => r.data),
  getDuty: (month: string) => http.get<DutyGrid>(`/months/${month}/duty`).then((r) => r.data),
  setDuty: (month: string, items: { store: string; date: string; salesperson: string }[]) =>
    http.put(`/months/${month}/duty`, { items }).then((r) => r.data),
  compute: (month: string) =>
    http.post<{ details: number; warnings: string[]; total: number }>(`/months/${month}/compute`).then((r) => r.data),
  getResults: (month: string) =>
    http.get<{ salary: { person: string; commission: number }[]; breakdown: any[]; stale: boolean }>(`/months/${month}/results`).then((r) => r.data),
  getSalesDetail: (month: string, store: string, person: string, date: string) =>
    http.get<{ items: {
      id: number; receipt: string; src_order: string | null; store: string; sale_date: string;
      barcode: string; product_name: string; qty: number; amount: number; unit_price: number;
      salesperson: string; cashier: string; is_return: boolean; is_online: boolean; tag: string;
      original_store: string | null; original_date: string | null; transfer_reason: string | null;
    }[] }>(
      `/months/${month}/sales-detail`, { params: { store, person, date } }
    ).then((r) => r.data),
  getTierDetail: (month: string, store: string, person: string, bucket: string) =>
    http.get<{ items: {
      id: number; receipt: string; src_order: string | null; store: string; sale_date: string;
      barcode: string; product_name: string; qty: number; amount: number; unit_price: number;
      salesperson: string; cashier: string; is_return: boolean; is_online: boolean; tag: string;
      original_store: string | null; original_date: string | null; transfer_reason: string | null;
    }[] }>(
      `/months/${month}/tier-detail`, { params: { store, person, bucket } }
    ).then((r) => r.data),
  getTierSummary: (month: string, store: string, person: string) =>
    http.get<{ tiers: { name: string; sales: number; qty: number; rate: number; rate_percent: string; commission: number }[]; total_sales: number; total_commission: number; bucket: string; target: number; monthly_target: number; duty_days: number }>(
      `/months/${month}/tier-summary`, { params: { store, person } }
    ).then((r) => r.data),
  downloadExport: async (month: string) => {
    const res = await http.get(`/months/${month}/export`, { responseType: "blob" });
    const url = URL.createObjectURL(res.data);
    const a = document.createElement("a");
    a.href = url; a.download = `salary_${month}.xlsx`; a.click();
    URL.revokeObjectURL(url);
  },
};

// —— 薪酬制度版本管理 ——
export interface SalaryPolicyContent {
  margin_rules: Record<string, any>;
  commission_rates: Record<string, any>;
}

export interface SalaryPolicyVersion {
  id: number;
  version: number;
  effective_from: string;
  is_current: boolean;
  created_at: string;
  created_by?: string;
  content: SalaryPolicyContent;
  note?: string;
}

export interface SalaryPolicySummary {
  id: number;
  version: number;
  effective_from: string;
  is_current: boolean;
  created_by?: string;
  note?: string;
  used_by_months: string[];
}

export const salaryPolicyApi = {
  list: () => http.get<SalaryPolicySummary[]>("/salary-policies").then(r => r.data),
  getCurrent: () => http.get<SalaryPolicyVersion>("/salary-policies/current").then(r => r.data),
  get: (id: number) => http.get<SalaryPolicyVersion>(`/salary-policies/${id}`).then(r => r.data),
  create: (data: {
    effective_from: string;
    note?: string;
    content: SalaryPolicyContent;
  }) => http.post<SalaryPolicyVersion>("/salary-policies", data).then(r => r.data),
  activate: (id: number) => http.post<SalaryPolicyVersion>(`/salary-policies/${id}/activate`).then(r => r.data),
  delete: (id: number) => http.delete(`/salary-policies/${id}`).then(r => r.data),
};


// —— 计算异常管理 ——
export interface Anomaly {
  id: number;
  month: string;
  anomaly_type: string;
  entity_type?: string;
  entity_id?: string;
  description: string;
  status: "pending" | "ignored" | "resolved";
  resolution?: string;
  created_at: string;
  resolved_at?: string;
}

export const anomalyApi = {
  list: (month: string, anomalyStatus?: string) =>
    http.get<Anomaly[]>(`/anomalies/month/${month}${anomalyStatus ? `?anomaly_status=${anomalyStatus}` : ""}`).then(r => r.data),
  resolve: (id: number, resolution?: string) =>
    http.post<Anomaly>(`/anomalies/${id}/resolve`, { resolution }).then(r => r.data),
  ignore: (id: number) =>
    http.post<Anomaly>(`/anomalies/${id}/ignore`).then(r => r.data),
  clear: (month: string) =>
    http.delete<{ deleted: number }>(`/anomalies/month/${month}`).then(r => r.data),
};

// —— 月份步骤状态 ——
export const monthStepApi = {
  update: (month: string, step: string, stepData?: Record<string, boolean>) =>
    http.put(`/months/${month}/step`, { step, step_data: stepData }).then(r => r.data),
  reset: (month: string) =>
    http.post(`/months/${month}/reset`).then(r => r.data),
};

// —— 排班拖拽 ——
export const dutyTransferApi = {
  transfer: (month: string, fromStore: string, toStore: string, date: string, salesperson: string) =>
    http.post(`/months/${month}/duty/transfer`, {
      from_store: fromStore,
      to_store: toStore,
      date,
      salesperson,
    }).then(r => r.data),
};

// —— 预检 ——
export const workflowApiExtended = {
  checkAnomalies: (month: string) =>
    http.post<{ total: number; anomalies: any[] }>(`/months/${month}/check-anomalies`).then(r => r.data),
  confirmGiftDeduction: (month: string, anomalyId: number) =>
    http.post<{ deduct_qty: number; deduct_amt: number }>(
      `/months/${month}/gift-deduction/confirm`, { anomaly_id: anomalyId }).then(r => r.data),
};

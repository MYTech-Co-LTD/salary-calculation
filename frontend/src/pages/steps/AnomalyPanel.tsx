import { useEffect, useState, type ReactNode } from "react";
import { anomalyApi, storesApi, productsApi, targetsApi, workflowApiExtended, type Anomaly } from "../../api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { toast } from "sonner";
import {
  AlertTriangle,
  CheckCircle2,
  Store,
  Package,
  Target,
  FileText,
  Gift,
  RotateCcw,
  Plus,
  Edit,
  X,
  Tag,
} from "lucide-react";

interface AnomalyPanelProps {
  month: string;
  onResolved: () => void;
}

const ANOMALY_TYPES: Record<string, { label: string; icon: ReactNode; color: string }> = {
  "1": { label: "门店不存在", icon: <Store className="w-5 h-5" />, color: "text-red-600 bg-red-50 border-red-200" },
  "2": { label: "商品不存在", icon: <Package className="w-5 h-5" />, color: "text-red-600 bg-red-50 border-red-200" },
  "3": { label: "门店无目标", icon: <Target className="w-5 h-5" />, color: "text-amber-600 bg-amber-50 border-amber-200" },
  "4": { label: "商品信息不完整", icon: <FileText className="w-5 h-5" />, color: "text-amber-600 bg-amber-50 border-amber-200" },
  "5": { label: "赠送未匹配", icon: <Gift className="w-5 h-5" />, color: "text-blue-600 bg-blue-50 border-blue-200" },
  "6": { label: "退款未关联", icon: <RotateCcw className="w-5 h-5" />, color: "text-blue-600 bg-blue-50 border-blue-200" },
  "7": { label: "赠送件数不符", icon: <Gift className="w-5 h-5" />, color: "text-blue-600 bg-blue-50 border-blue-200" },
};

// 从描述中提取信息
function parseDescription(desc: string): Record<string, string> {
  const info: Record<string, string> = {};
  const patterns = [
    { key: "name", regex: /商品名:\s*([^|]+)/ },
    { key: "category", regex: /类别:\s*([^|]+)/ },
    { key: "spec", regex: /规格:\s*([^|]+)/ },
    { key: "cost", regex: /成本:\s*([^|]+)/ },
    { key: "group", regex: /组别:\s*([^|]+)/ },
    { key: "store_class", regex: /类别:\s*([^|]+)/ },
    { key: "salespersons", regex: /涉及营业员:\s*([^|]+)/ },
    { key: "count", regex: /交易笔数:\s*(\d+)/ },
    { key: "receipt", regex: /单号:\s*([^|]+)/ },
    { key: "barcode", regex: /条码:\s*([^|]+)/ },
    { key: "sales_qty", regex: /销售件数:\s*([^|]+)/ },
    { key: "gift_qty", regex: /赠送件数:\s*([^|]+)/ },
  ];
  for (const p of patterns) {
    const match = desc.match(p.regex);
    if (match) info[p.key] = match[1].trim();
  }
  return info;
}

export default function AnomalyPanel({ month, onResolved }: AnomalyPanelProps) {
  const [anomalies, setAnomalies] = useState<Anomaly[]>([]);
  const [loading, setLoading] = useState(false);
  const [expandedType, setExpandedType] = useState<string | null>(null);
  const [editStore, setEditStore] = useState<{ open: boolean; name: string; group: string; storeClass: string; excludeAssessment: boolean }>({ open: false, name: "", group: "", storeClass: "", excludeAssessment: false });
  const [editProduct, setEditProduct] = useState<{ open: boolean; barcode: string; name: string; category: string; cost: string; excludeCommission: boolean }>({ open: false, barcode: "", name: "", category: "", cost: "", excludeCommission: false });
  const [editTarget, setEditTarget] = useState<{ open: boolean; store: string; target: string }>({ open: false, store: "", target: "" });

  const load = async () => {
    setLoading(true);
    try {
      const data = await anomalyApi.list(month, "pending");
      setAnomalies(data);
    } catch {
      toast.error("加载异常失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [month]);

  // 类型1: 快速创建门店
  const handleCreateStore = (anomaly: Anomaly) => {
    const info = parseDescription(anomaly.description);
    setEditStore({
      open: true,
      name: anomaly.entity_id || "",
      group: info.group || "",
      storeClass: info.store_class || "",
      excludeAssessment: false,
    });
  };

  const handleSaveStore = async () => {
    try {
      await storesApi.upsert({
        name: editStore.name,
        group: editStore.group || undefined,
        store_class: editStore.storeClass || undefined,
        exclude_assessment: editStore.excludeAssessment,
      } as any);
      toast.success(`门店「${editStore.name}」已创建`);
      setEditStore({ open: false, name: "", group: "", storeClass: "", excludeAssessment: false });
      const anomaly = anomalies.find(a => a.anomaly_type === "1" && a.entity_id === editStore.name);
      if (anomaly) await anomalyApi.ignore(anomaly.id);
      load();
      onResolved();
    } catch {
      toast.error("创建门店失败");
    }
  };

  // 类型2: 快速创建商品
  const handleCreateProduct = (anomaly: Anomaly) => {
    const info = parseDescription(anomaly.description);
    setEditProduct({
      open: true,
      barcode: anomaly.entity_id || "",
      name: info.name || "",
      category: info.category || "",
      cost: "",
      excludeCommission: false,
    });
  };

  const handleSaveProduct = async () => {
    try {
      await productsApi.upsert({
        barcode: editProduct.barcode,
        name: editProduct.name || undefined,
        category: editProduct.category || undefined,
        cost: editProduct.cost ? Number(editProduct.cost) : undefined,
        exclude_commission: editProduct.excludeCommission,
      } as any);
      toast.success(`商品「${editProduct.barcode}」已创建`);
      setEditProduct({ open: false, barcode: "", name: "", category: "", cost: "", excludeCommission: false });
      const anomaly = anomalies.find(a => a.anomaly_type === "2" && a.entity_id === editProduct.barcode);
      if (anomaly) await anomalyApi.ignore(anomaly.id);
      load();
      onResolved();
    } catch {
      toast.error("创建商品失败");
    }
  };

  // 类型3: 填目标
  const handleFillTarget = (store: string) => {
    setEditTarget({ open: true, store, target: "" });
  };

  const handleSaveTarget = async () => {
    try {
      await targetsApi.set(month, [{ store: editTarget.store, target: editTarget.target }]);
      toast.success(`「${editTarget.store}」目标已设置`);
      setEditTarget({ open: false, store: "", target: "" });
      const anomaly = anomalies.find(a => a.anomaly_type === "3" && a.entity_id === editTarget.store);
      if (anomaly) await anomalyApi.ignore(anomaly.id);
      load();
      onResolved();
    } catch {
      toast.error("设置目标失败");
    }
  };

  // 类型3: 标记不参与考核
  const handleExcludeAssessment = async (store: string) => {
    try {
      console.log('handleExcludeAssessment called with store:', store);
      const result = await storesApi.patch(store, { exclude_assessment: true });
      console.log('patch result:', result);
      toast.success(`「${store}」已标记不参与考核`);
      const anomaly = anomalies.find(a => a.anomaly_type === "3" && a.entity_id === store);
      console.log('found anomaly:', anomaly);
      if (anomaly) {
        console.log('ignoring anomaly:', anomaly.id);
        await anomalyApi.ignore(anomaly.id);
      }
      load();
      onResolved();
    } catch (e) {
      console.error('handleExcludeAssessment error:', e);
      toast.error("操作失败");
    }
  };

  // 类型4: 编辑商品
  const handleEditProduct = (anomaly: Anomaly) => {
    const info = parseDescription(anomaly.description);
    setEditProduct({
      open: true,
      barcode: anomaly.entity_id || "",
      name: info.name || "",
      category: info.category || "",
      cost: info.cost || "",
      excludeCommission: false,
    });
  };

  const handleSaveProductEdit = async () => {
    try {
      await productsApi.upsert({
        barcode: editProduct.barcode,
        category: editProduct.category || undefined,
        cost: editProduct.cost ? Number(editProduct.cost) : undefined,
        exclude_commission: editProduct.excludeCommission,
      } as any);
      toast.success(`商品「${editProduct.barcode}」已更新`);
      setEditProduct({ open: false, barcode: "", name: "", category: "", cost: "", excludeCommission: false });
      const anomaly = anomalies.find(a => a.anomaly_type === "4" && a.entity_id === editProduct.barcode);
      if (anomaly) await anomalyApi.ignore(anomaly.id);
      load();
      onResolved();
    } catch {
      toast.error("更新商品失败");
    }
  };

  // 类型4: 标记不计提成
  const handleExcludeCommission = async (barcode: string) => {
    try {
      console.log('handleExcludeCommission called with barcode:', barcode);
      const result = await productsApi.patch(barcode, { exclude_commission: true });
      console.log('patch result:', result);
      toast.success(`商品「${barcode}」已标记不计提成`);
      const anomaly = anomalies.find(a => a.anomaly_type === "4" && a.entity_id === barcode);
      console.log('found anomaly:', anomaly);
      if (anomaly) {
        console.log('ignoring anomaly:', anomaly.id);
        await anomalyApi.ignore(anomaly.id);
      }
      load();
      onResolved();
    } catch (e) {
      console.error('handleExcludeCommission error:', e);
      toast.error("操作失败");
    }
  };

  // 类型5/6: 忽略
  const handleIgnore = async (id: number) => {
    try {
      await anomalyApi.ignore(id);
      toast.success("已忽略");
      load();
      onResolved();
    } catch {
      toast.error("忽略失败");
    }
  };

  // 类型6: 扣减当日营业额
  const handleDeductSales = async (id: number) => {
    try {
      await anomalyApi.resolve(id, "扣减当日营业额");
      toast.success("已扣减当日营业额");
      load();
      onResolved();
    } catch {
      toast.error("扣减失败");
    }
  };

  // 类型7: 确认按件数扣除
  const handleConfirmDeduct = async (item: Anomaly) => {
    try {
      const r = await workflowApiExtended.confirmGiftDeduction(month, item.id);
      toast.success(`已确认扣除 ${r.deduct_qty} 件，扣除额 ${r.deduct_amt} 元`);
      load();
      onResolved();
    } catch {
      toast.error("确认扣除失败");
    }
  };

  // 按类型统计
  const typeCounts: Record<string, number> = {};
  Object.keys(ANOMALY_TYPES).forEach((t) => (typeCounts[t] = 0));
  anomalies.forEach((a) => {
    typeCounts[a.anomaly_type] = (typeCounts[a.anomaly_type] || 0) + 1;
  });

  // 按类型分组
  const grouped: Record<string, Anomaly[]> = {};
  anomalies.forEach((a) => {
    grouped[a.anomaly_type] = grouped[a.anomaly_type] || [];
    grouped[a.anomaly_type].push(a);
  });

  if (loading) {
    return <div className="text-sm text-zinc-400">加载异常...</div>;
  }

  return (
    <div className="space-y-3">
      {/* 异常类型看板 */}
      <div className="grid grid-cols-3 gap-2">
        {Object.entries(ANOMALY_TYPES).map(([type, info]) => {
          const count = typeCounts[type] || 0;
          const isExpanded = expandedType === type;
          return (
            <button
              key={type}
              onClick={() => setExpandedType(isExpanded ? null : type)}
              className={`rounded-lg border p-3 text-left transition-all ${
                count > 0
                  ? `${info.color} cursor-pointer hover:shadow-sm`
                  : "text-zinc-400 bg-zinc-50 border-zinc-200"
              }`}
            >
              <div className="flex items-center justify-between">
                <span className={count > 0 ? "" : "text-zinc-300"}>{info.icon}</span>
                <span className={`text-lg font-semibold ${count > 0 ? "" : "text-zinc-300"}`}>
                  {count}
                </span>
              </div>
              <p className="text-xs mt-1.5 font-medium">{info.label}</p>
            </button>
          );
        })}
      </div>

      {/* 展开的异常详情 */}
      {expandedType && typeCounts[expandedType] > 0 && (
        <div className="border rounded-lg overflow-hidden">
          <div className={`px-4 py-2 text-sm font-medium flex items-center gap-2 ${ANOMALY_TYPES[expandedType]?.color || ""}`}>
            {ANOMALY_TYPES[expandedType]?.icon}
            <span>{ANOMALY_TYPES[expandedType]?.label}</span>
            <span className="opacity-70 font-normal">({typeCounts[expandedType]})</span>
          </div>
          <div className="divide-y max-h-80 overflow-y-auto">
            {(grouped[expandedType] || []).map((item) => {
              const info = parseDescription(item.description);
              return (
                <div key={item.id} className="px-4 py-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium">{item.entity_id}</p>
                      {/* 显示解析出的详细信息 */}
                      <div className="flex flex-wrap gap-x-3 gap-y-0.5 mt-1">
                        {info.name && <span className="text-xs text-zinc-500">名称: {info.name}</span>}
                        {info.category && <span className="text-xs text-zinc-500">类别: {info.category}</span>}
                        {info.spec && <span className="text-xs text-zinc-500">规格: {info.spec}</span>}
                        {info.cost && <span className="text-xs text-zinc-500">成本: {info.cost}</span>}
                        {info.group && <span className="text-xs text-zinc-500">组别: {info.group}</span>}
                        {info.store_class && <span className="text-xs text-zinc-500">类别: {info.store_class}</span>}
                        {info.salespersons && <span className="text-xs text-zinc-500">营业员: {info.salespersons}</span>}
                        {info.count && <span className="text-xs text-zinc-500">笔数: {info.count}</span>}
                        {info.sales_qty && <span className="text-xs text-zinc-500">销售件数: {info.sales_qty}</span>}
                        {info.gift_qty && <span className="text-xs text-zinc-500">赠送件数: {info.gift_qty}</span>}
                      </div>
                    </div>
                    <div className="flex gap-1.5 shrink-0">
                      {/* 类型1: 创建门店 */}
                      {expandedType === "1" && (
                        <Button size="sm" onClick={() => handleCreateStore(item)}>
                          <Plus className="w-3 h-3 mr-1" />创建门店
                        </Button>
                      )}
                      {/* 类型2: 创建商品 */}
                      {expandedType === "2" && (
                        <Button size="sm" onClick={() => handleCreateProduct(item)}>
                          <Plus className="w-3 h-3 mr-1" />创建商品
                        </Button>
                      )}
                      {/* 类型3: 填目标 / 不参与考核 */}
                      {expandedType === "3" && (
                        <>
                          <Button size="sm" onClick={() => handleFillTarget(item.entity_id!)}>
                            <Target className="w-3 h-3 mr-1" />填目标
                          </Button>
                          <Button size="sm" variant="outline" onClick={() => handleExcludeAssessment(item.entity_id!)}>
                            <Tag className="w-3 h-3 mr-1" />不参与考核
                          </Button>
                        </>
                      )}
                      {/* 类型4: 编辑商品 / 不计提成 */}
                      {expandedType === "4" && (
                        <>
                          <Button size="sm" onClick={() => handleEditProduct(item)}>
                            <Edit className="w-3 h-3 mr-1" />编辑
                          </Button>
                          <Button size="sm" variant="outline" onClick={() => handleExcludeCommission(item.entity_id!)}>
                            <Tag className="w-3 h-3 mr-1" />不计提成
                          </Button>
                        </>
                      )}
                      {/* 类型5: 忽略 */}
                      {expandedType === "5" && (
                        <Button size="sm" variant="outline" onClick={() => handleIgnore(item.id)}>
                          <X className="w-3 h-3 mr-1" />忽略
                        </Button>
                      )}
                      {/* 类型6: 忽略 / 扣减营业额 */}
                      {expandedType === "6" && (
                        <>
                          <Button size="sm" variant="outline" onClick={() => handleIgnore(item.id)}>
                            <X className="w-3 h-3 mr-1" />忽略
                          </Button>
                          <Button size="sm" onClick={() => handleDeductSales(item.id)}>
                            <RotateCcw className="w-3 h-3 mr-1" />扣减营业额
                          </Button>
                        </>
                      )}
                      {/* 类型7: 确认扣除 / 忽略 */}
                      {expandedType === "7" && (
                        <>
                          <Button size="sm" variant="outline" onClick={() => handleIgnore(item.id)}>
                            <X className="w-3 h-3 mr-1" />忽略
                          </Button>
                          <Button size="sm" onClick={() => handleConfirmDeduct(item)}>
                            <Gift className="w-3 h-3 mr-1" />确认扣除
                          </Button>
                        </>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* 总计提示 */}
      {anomalies.length > 0 ? (
        <div className="flex items-center gap-2 text-sm text-amber-700 bg-amber-50 rounded-lg px-4 py-3">
          <AlertTriangle className="w-4 h-4" />
          共 {anomalies.length} 个异常待处理
        </div>
      ) : (
        <div className="flex items-center gap-2 text-sm text-emerald-600 bg-emerald-50 rounded-lg px-4 py-3">
          <CheckCircle2 className="w-4 h-4" />
          无待处理异常，可以继续计算
        </div>
      )}

      {/* 创建门店弹窗 */}
      <Dialog open={editStore.open} onOpenChange={(open) => setEditStore({ ...editStore, open })}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader><DialogTitle>创建门店</DialogTitle></DialogHeader>
          <div className="space-y-3 py-2">
            <div><Label>门店名称</Label><Input value={editStore.name} disabled className="mt-1 h-9" /></div>
            <div><Label>所属组</Label><Input value={editStore.group} onChange={e => setEditStore({ ...editStore, group: e.target.value })} placeholder="如：1组" className="mt-1 h-9" /></div>
            <div><Label>门店类别</Label><Input value={editStore.storeClass} onChange={e => setEditStore({ ...editStore, storeClass: e.target.value })} placeholder="如：A/B/C/D" className="mt-1 h-9" /></div>
            <div className="flex items-center justify-between">
              <Label>不参与考核</Label>
              <Button
                variant={editStore.excludeAssessment ? "default" : "outline"}
                size="sm"
                onClick={() => setEditStore({ ...editStore, excludeAssessment: !editStore.excludeAssessment })}
              >
                {editStore.excludeAssessment ? "是" : "否"}
              </Button>
            </div>
          </div>
          <DialogFooter><Button onClick={handleSaveStore}>创建</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 创建/编辑商品弹窗 */}
      <Dialog open={editProduct.open} onOpenChange={(open) => setEditProduct({ ...editProduct, open })}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader><DialogTitle>{expandedType === "2" ? "创建商品" : "编辑商品"}</DialogTitle></DialogHeader>
          <div className="space-y-3 py-2">
            <div><Label>条码</Label><Input value={editProduct.barcode} disabled className="mt-1 h-9" /></div>
            <div><Label>商品名称</Label><Input value={editProduct.name} onChange={e => setEditProduct({ ...editProduct, name: e.target.value })} placeholder="从销售数据自动带入" className="mt-1 h-9" /></div>
            <div>
              <Label>类别</Label>
              <Select value={editProduct.category || "_empty"} onValueChange={v => setEditProduct({ ...editProduct, category: v === "_empty" ? "" : v })}>
                <SelectTrigger className="mt-1 h-9 bg-white">
                  <SelectValue placeholder="选择类别" />
                </SelectTrigger>
                <SelectContent className="bg-white">
                  <SelectItem value="_empty">空</SelectItem>
                  <SelectItem value="常温奶">常温奶</SelectItem>
                  <SelectItem value="低温奶">低温奶</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div><Label>销售成本</Label><Input type="number" value={editProduct.cost} onChange={e => setEditProduct({ ...editProduct, cost: e.target.value })} placeholder="0.00" className="mt-1 h-9" /></div>
            <div className="flex items-center justify-between">
              <Label>不计提成</Label>
              <Button
                variant={editProduct.excludeCommission ? "default" : "outline"}
                size="sm"
                onClick={() => setEditProduct({ ...editProduct, excludeCommission: !editProduct.excludeCommission })}
              >
                {editProduct.excludeCommission ? "是" : "否"}
              </Button>
            </div>
          </div>
          <DialogFooter><Button onClick={expandedType === "2" ? handleSaveProduct : handleSaveProductEdit}>{expandedType === "2" ? "创建" : "保存"}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 填目标弹窗 */}
      <Dialog open={editTarget.open} onOpenChange={(open) => setEditTarget({ ...editTarget, open })}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader><DialogTitle>设置目标</DialogTitle></DialogHeader>
          <div className="space-y-3 py-2">
            <div><Label>门店</Label><Input value={editTarget.store} disabled className="mt-1 h-9" /></div>
            <div><Label>月度目标</Label><Input type="number" value={editTarget.target} onChange={e => setEditTarget({ ...editTarget, target: e.target.value })} placeholder="0" className="mt-1 h-9" /></div>
          </div>
          <DialogFooter><Button onClick={handleSaveTarget}>保存</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

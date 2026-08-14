import { useEffect, useState } from "react";
import { workflowApi, workflowApiExtended, putFileToOss, salaryPolicyApi, monthStepApi, monthsApi } from "../../api";
import { Badge } from "@/components/ui/badge";
import { toast } from "sonner";
import { Upload } from "lucide-react";

function DropZone({
  label,
  onUpload,
  done,
  progress,
}: {
  label: string;
  onUpload: (f: File) => void;
  done: boolean;
  progress?: number | null;
}) {
  const uploading = progress != null;
  return (
    <div
      className={`rounded-lg border-2 border-dashed p-5 text-center transition-colors cursor-pointer ${
        done
          ? "border-emerald-300 bg-emerald-50"
          : "border-zinc-200 hover:border-zinc-300 bg-white"
      }`}
    >
      <label className="cursor-pointer flex flex-col items-center gap-2">
        <Upload
          className={`w-5 h-5 ${
            done
              ? "text-emerald-500"
              : uploading
              ? "text-blue-500 animate-pulse"
              : "text-zinc-400"
          }`}
        />
        <span className="text-sm text-zinc-500">
          {uploading ? `上传中 ${progress}%` : label}
          {done && !uploading && (
            <Badge className="ml-1 bg-emerald-100 text-emerald-700 border-emerald-200">
              已上传
            </Badge>
          )}
        </span>
        <input
          type="file"
          accept=".xlsx,.xls"
          className="sr-only"
          disabled={uploading}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) onUpload(f);
          }}
        />
      </label>
    </div>
  );
}

export default function ImportStep({ month }: { month: string }) {
  const [sales, setSales] = useState(false);
  const [gifts, setGifts] = useState(false);
  const [uploading, setUploading] = useState<{ kind: string; progress: number } | null>(null);
  const [policyInfo, setPolicyInfo] = useState<{
    version: number;
    effective_from: string;
  } | null>(null);

  useEffect(() => {
    // 加载月份信息，恢复已上传状态
    monthsApi.get(month).then((m) => {
      if (m.sales_file) setSales(true);
      if (m.gifts_file) setGifts(true);
    }).catch(() => {});
    // 加载薪酬制度信息
    salaryPolicyApi
      .getCurrent()
      .then((p) =>
        setPolicyInfo({ version: p.version, effective_from: p.effective_from })
      )
      .catch(() => {});
  }, [month]);

  // function 声明：legacyUpload 先于 markDone 出现，避免 const 箭头函数先用后声明
  function legacyUpload(kind: "sales" | "gifts", file: File) {
    setUploading({ kind, progress: 0 });
    return (kind === "sales" ? workflowApi.importSales : workflowApi.importGifts)(month, file)
      .then(() => markDone(kind))
      .catch(() => toast.error("上传失败，请检查网络后重试"))
      .finally(() => setUploading(null));
  }

  function markDone(kind: "sales" | "gifts") {
    kind === "sales" ? setSales(true) : setGifts(true);
    toast.success(`${kind === "sales" ? "销售流水" : "让利明细"}上传成功`);
    const newSales = kind === "sales" ? true : sales;
    const newGifts = kind === "gifts" ? true : gifts;
    if (newSales && newGifts) {
      monthStepApi.update(month, "import", { import: true }).catch(() => {});
    }
  }

  async function upload(kind: "sales" | "gifts", file: File) {
    setUploading({ kind, progress: 0 });
    let ticket: { url: string; key: string };
    try {
      ticket = await workflowApiExtended.getUploadTicket(month, kind);
    } catch {
      // OBS 通道不可用（未配置/网络）→ 回退旧 multipart 直传
      return legacyUpload(kind, file);
    }
    try {
      await putFileToOss(ticket.url, file, (p) => setUploading({ kind, progress: p }));
      await workflowApiExtended.importFromOss(month, kind, ticket.key);
      markDone(kind);
    } catch (e: any) {
      const detail = e?.response?.data?.detail;
      toast.error(detail ? String(detail) : "上传失败，请重新选择文件重试");
    } finally {
      setUploading(null);
    }
  }

  return (
    <div className="space-y-3 max-w-xl">
      {/* 薪酬制度版本信息 */}
      {policyInfo && (
        <div className="rounded-lg bg-green-50 border border-green-200 px-4 py-3 text-sm">
          <div className="flex items-center gap-2">
            <span className="text-green-600">📋</span>
            <span className="font-medium text-green-800">
              薪酬制度 v{policyInfo.version}
            </span>
            <span className="text-green-600">
              （生效日期：{policyInfo.effective_from}）
            </span>
            <Badge className="bg-green-100 text-green-700 border-green-200">
              自动关联
            </Badge>
          </div>
        </div>
      )}

      <DropZone
        label="上传销售流水 xlsx"
        onUpload={(f) => upload("sales", f)}
        done={sales}
        progress={uploading?.kind === "sales" ? uploading.progress : null}
      />
      <DropZone
        label="上传让利明细 xlsx（赠送清单，可选）"
        onUpload={(f) => upload("gifts", f)}
        done={gifts}
        progress={uploading?.kind === "gifts" ? uploading.progress : null}
      />
    </div>
  );
}

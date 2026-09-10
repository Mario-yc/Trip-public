import { useState } from "react";
import { SourceCleanupResponse } from "../../services/apiClient";

type ImageRetentionControlProps = {
  onCleanup: () => Promise<SourceCleanupResponse>;
};

export function ImageRetentionControl({ onCleanup }: ImageRetentionControlProps) {
  const [result, setResult] = useState<SourceCleanupResponse | null>(null);
  const [errorMessage, setErrorMessage] = useState("");

  async function handleCleanup() {
    setErrorMessage("");
    try {
      setResult(await onCleanup());
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "清理失败，请稍后重试");
    }
  }

  return (
    <section className="utility-panel" aria-label="Image retention cleanup">
      <h2>图片缓存</h2>
      <button type="button" onClick={handleCleanup}>
        手动清理临时原图
      </button>
      {errorMessage ? <p role="alert">{errorMessage}</p> : null}
      {result ? (
        <p>
          已清理 {result.clearedCount} 个临时原图，长期保存 {result.retainedLongTermCount} 个。
        </p>
      ) : null}
    </section>
  );
}

import type { SharedTravelSource } from "../../services/apiClient";
import { safeExternalHttpUrl } from "../../services/safeExternalUrl";

export function SharedTravelSourceCard({ source }: { source: SharedTravelSource }) {
  const bodyAvailable = source.status === "completed" && Boolean(source.bodyText?.trim());
  const sourceUrl = safeExternalHttpUrl(source.canonicalUrl);

  return (
    <section aria-label="分享资料原文" className="shared-travel-source">
      <header>
        <strong>{source.title || "分享资料"}</strong>
        <span className="agent-guide-readonly-badge">只读 · 不改行程</span>
      </header>
      <p>{bodyAvailable ? "已读取文字正文" : "未能读取公开正文"}</p>
      {bodyAvailable ? (
        <details>
          <summary>查看文字正文</summary>
          <div className="shared-travel-source-body">{source.bodyText}</div>
        </details>
      ) : (
        <p>请粘贴分享文字或笔记正文补充资料。</p>
      )}
      <p className="shared-travel-source-images">
        {source.imageCount > 0 ? `检测到 ${source.imageCount} 张图片，图片尚未解读。` : "图片尚未解读。"}
      </p>
      {sourceUrl ? <a href={sourceUrl} target="_blank" rel="noreferrer noopener">打开来源页面</a> : null}
    </section>
  );
}

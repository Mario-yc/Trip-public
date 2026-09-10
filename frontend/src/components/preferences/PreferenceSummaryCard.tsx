import { useEffect, useMemo, useState } from "react";
import { Pin } from "lucide-react";
import { PreferenceMemory, PreferenceSummaryCard as PreferenceSummaryCardType } from "../../services/apiClient";

type PreferenceSummaryCardProps = {
  card: PreferenceSummaryCardType | null;
  memory: PreferenceMemory | null;
  errorMessage?: string;
  collapsed?: boolean;
  onChange: (card: PreferenceSummaryCardType) => void;
  onSave?: (memoryText: string, autoUpdateEnabled: boolean) => Promise<PreferenceMemory | null>;
  onRestoreDefault?: () => Promise<PreferenceMemory | null>;
};

const DEFAULT_MEMORY_TEXT = `# 我的旅行偏好

## 旅行节奏
- 暂无明确记录。

## 兴趣偏好
- 暂无明确记录。

## 餐饮偏好
- 暂无明确记录。

## 交通偏好
- 暂无明确记录。

## 住宿偏好
- 暂无明确记录。

## 预算偏好
- 暂无明确记录。

## 同行与特殊需求
- 暂无明确记录。

## 对话风格
- 暂无明确记录。

## 需要确认
- 暂无明确记录。
`;

const DEFAULT_CARD: PreferenceSummaryCardType = {
  id: "local_preference_card",
  profileId: "local_preference_profile",
  partySize: 0,
  travelerTypes: [],
  budgetRange: "",
  pacePreference: "",
  summaryText: "",
  items: [],
  status: "editable",
  providerName: "local-default",
  fallbackUsed: false
};

export function PreferenceSummaryCard({
  card,
  memory,
  onChange,
  onSave,
  onRestoreDefault,
  errorMessage = "",
  collapsed = false
}: PreferenceSummaryCardProps) {
  const editableCard = card ? normalizePreferenceCard(card) : DEFAULT_CARD;
  const currentMemoryText = memory?.memoryText ?? markdownFromLegacyCard(editableCard);
  const [isEditing, setIsEditing] = useState(false);
  const [draftText, setDraftText] = useState(currentMemoryText);
  const [autoUpdateEnabled, setAutoUpdateEnabled] = useState(memory?.autoUpdateEnabled ?? true);
  const [isSaving, setIsSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState("");
  const visibleSections = useMemo(() => visibleMarkdownSections(currentMemoryText), [currentMemoryText]);
  const structuredFacts = memory?.structuredMemory?.facts ?? [];
  const pendingConfirmations = memory?.pendingConfirmations ?? [];

  useEffect(() => {
    setDraftText(currentMemoryText);
    setAutoUpdateEnabled(memory?.autoUpdateEnabled ?? true);
  }, [currentMemoryText, memory?.autoUpdateEnabled]);

  async function handleSave() {
    setSaveMessage("");
    setIsSaving(true);
    try {
      const saved = await onSave?.(draftText, autoUpdateEnabled);
      if (saved) {
        onChange({ ...editableCard, summaryText: saved.memoryText });
      }
      setIsEditing(false);
      setSaveMessage("已保存，后续规划会读取这份偏好卡片。");
    } catch (error) {
      setSaveMessage(error instanceof Error ? error.message : "偏好保存失败。");
    } finally {
      setIsSaving(false);
    }
  }

  async function handleRestoreDefault() {
    setSaveMessage("");
    setIsSaving(true);
    try {
      const restored = await onRestoreDefault?.();
      if (restored) {
        onChange({ ...editableCard, summaryText: restored.memoryText });
        setDraftText(restored.memoryText);
        setAutoUpdateEnabled(restored.autoUpdateEnabled);
      }
      setIsEditing(false);
      setSaveMessage("已恢复默认模板。");
    } catch (error) {
      setSaveMessage(error instanceof Error ? error.message : "恢复默认模板失败。");
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <section className="preference-card" aria-label="Preference summary card">
      <div className="note-header">
        <Pin aria-hidden="true" className="preference-card-pin" size={20} strokeWidth={2.2} />
        <h2>旅行偏好卡片</h2>
        <span>可手动编辑</span>
      </div>
      {collapsed ? <p>双击展开编辑旅行偏好</p> : null}
      {collapsed ? null : (
        <>
          {isEditing ? (
            <div className="preference-summary natural-preference-summary">
              <label>
                <span>旅行偏好 Markdown</span>
                <textarea
                  aria-label="旅行偏好 Markdown"
                  rows={14}
                  value={draftText}
                  onChange={(event) => setDraftText(event.target.value)}
                />
              </label>
              <label className="checkbox-row">
                <input
                  checked={autoUpdateEnabled}
                  type="checkbox"
                  onChange={(event) => setAutoUpdateEnabled(event.target.checked)}
                />
                <span>允许 Agent 根据对话自动更新我的旅行偏好</span>
              </label>
              <div className="preference-actions">
                <button disabled={isSaving} type="button" onClick={handleSave}>
                  {isSaving ? "保存中..." : "保存偏好卡片"}
                </button>
                <button disabled={isSaving} type="button" onClick={handleRestoreDefault}>
                  恢复默认模板
                </button>
                <button disabled={isSaving} type="button" onClick={() => setIsEditing(false)}>
                  取消
                </button>
              </div>
            </div>
          ) : (
            <div className="preference-markdown-view">
              {visibleSections.length ? (
                visibleSections.map((section) => (
                  <section key={section.title}>
                    <h3>{section.title}</h3>
                    <ul>
                      {section.items.map((item) => (
                        <li key={`${section.title}-${item}`}>{item}</li>
                      ))}
                    </ul>
                  </section>
                ))
              ) : (
                <p>还没有记录明确旅行偏好，Agent 会在后续对话中逐步整理。</p>
              )}
              {structuredFacts.length ? (
                <section>
                  <h3>结构化记忆</h3>
                  <ul>
                    {structuredFacts.slice(0, 6).map((fact) => (
                      <li key={fact.id}>
                        [{fact.scope}/{fact.status}] {fact.value}
                      </li>
                    ))}
                  </ul>
                </section>
              ) : null}
              {pendingConfirmations.length ? (
                <section>
                  <h3>需要确认</h3>
                  <ul>
                    {pendingConfirmations.slice(0, 4).map((item, index) => (
                      <li key={`${String(item.factId ?? index)}-${String(item.category ?? "")}`}>
                        {String(item.value ?? "")}
                      </li>
                    ))}
                  </ul>
                </section>
              ) : null}
              <button type="button" onClick={() => setIsEditing(true)}>
                编辑偏好卡片
              </button>
            </div>
          )}
          {errorMessage ? <p role="alert">{errorMessage}</p> : null}
          {saveMessage ? <p aria-live="polite">{saveMessage}</p> : null}
        </>
      )}
    </section>
  );
}

export function defaultPreferenceCard() {
  return DEFAULT_CARD;
}

export function defaultPreferenceMemory(): PreferenceMemory {
  return {
    userId: "default",
    memoryText: DEFAULT_MEMORY_TEXT,
    structuredMemory: { version: "travel-memory-v1", facts: [], autoUpdateClassifications: [] },
    compiledRules: {},
    pendingConfirmations: [],
    autoUpdateEnabled: true,
    createdAt: "",
    updatedAt: ""
  };
}

function normalizePreferenceCard(card: PreferenceSummaryCardType): PreferenceSummaryCardType {
  return {
    ...DEFAULT_CARD,
    ...card
  };
}

function markdownFromLegacyCard(card: PreferenceSummaryCardType) {
  if (!card.summaryText && !card.items.length && !card.pacePreference && !card.budgetRange) {
    return DEFAULT_MEMORY_TEXT;
  }
  const sections = [
    "# 我的旅行偏好",
    "",
    "## 旅行节奏",
    card.pacePreference ? `- ${card.pacePreference}` : "- 暂无明确记录。",
    "",
    "## 兴趣偏好",
    card.items.length ? card.items.map((item) => `- ${item.label}`).join("\n") : "- 暂无明确记录。",
    "",
    "## 预算偏好",
    card.budgetRange ? `- ${card.budgetRange}` : "- 暂无明确记录。",
    "",
    "## 需要确认",
    card.summaryText ? `- ${card.summaryText}` : "- 暂无明确记录。"
  ];
  return sections.join("\n");
}

function visibleMarkdownSections(markdown: string) {
  const sections: Array<{ title: string; items: string[] }> = [];
  let current: { title: string; items: string[] } | null = null;
  for (const rawLine of markdown.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (line.startsWith("## ")) {
      if (current && current.items.length) {
        sections.push(current);
      }
      current = { title: line.slice(3).trim(), items: [] };
      continue;
    }
    if (!current || !line.startsWith("- ")) {
      continue;
    }
    const item = line.slice(2).trim();
    if (item && item !== "暂无明确记录。") {
      current.items.push(item);
    }
  }
  if (current && current.items.length) {
    sections.push(current);
  }
  return sections;
}

export function effectivePreferenceMemoryText(markdown?: string | null) {
  if (!markdown) {
    return "";
  }
  const sections = visibleMarkdownSections(markdown);
  if (!sections.length) {
    const plain = markdown
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith("#") && line !== "暂无明确记录。" && line !== "- 暂无明确记录。")
      .join("\n");
    return plain === DEFAULT_MEMORY_TEXT.trim() ? "" : plain;
  }
  return sections
    .flatMap((section) => section.items.map((item) => `${section.title}：${item}`))
    .join("\n")
    .trim();
}

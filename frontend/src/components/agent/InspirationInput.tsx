import { type DragEvent, type FormEvent, type KeyboardEvent, useEffect, useState } from "react";
import { InspirationInputPayload } from "../../services/apiClient";

type InspirationInputProps = {
  cityHint?: string;
  disabled?: boolean;
  onCityChange?: (city: string) => void;
  onSubmit: (payload: InspirationInputPayload) => boolean | void | Promise<boolean | void>;
};

const URL_PATTERN = /(https?:\/\/[^\s]+)/g;

const COPIED_CHAT_CHROME_PREFIX = ["对话", "复制测试信息", "‹"];

export function normalizeAgentInputText(value: string): string {
  const normalized = value.replace(/\r\n?/g, "\n");
  const lines = normalized.split("\n");
  const hasExactChromePrefix = COPIED_CHAT_CHROME_PREFIX.every((item, index) => lines[index]?.trim() === item);
  return (hasExactChromePrefix ? lines.slice(COPIED_CHAT_CHROME_PREFIX.length).join("\n") : normalized).trim();
}


export function InspirationInput({ cityHint: externalCityHint, disabled = false, onCityChange, onSubmit }: InspirationInputProps) {
  const [cityHint, setCityHint] = useState(externalCityHint ?? "北京");
  const [text, setText] = useState("");
  const [link, setLink] = useState("");
  const [sourceKind] = useState("screenshot");
  const [files, setFiles] = useState<File[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const [inputError, setInputError] = useState("");
  const [saveOriginalImages] = useState(false);

  useEffect(() => {
    if (externalCityHint) {
      setCityHint(externalCityHint);
    }
  }, [externalCityHint]);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (disabled || (!text.trim() && !link.trim() && !files.length)) {
      return;
    }
    const normalizedText = normalizeAgentInputText(text);
    const detectedLinks = normalizedText.match(URL_PATTERN) ?? [];
    if (files.length && (link.trim() || detectedLinks.length)) {
      setInputError("链接和文件请分开发送；本次未提交，文字、链接和文件都已保留。");
      return;
    }
    setInputError("");
    const payload = {
      cityHint,
      textItems: normalizedText ? [normalizedText] : [],
      socialLinks: [...(link ? [link] : []), ...detectedLinks],
      files,
      sourceKind,
      saveOriginalImages
    };
    setText("");
    setLink("");
    setFiles([]);
    await onSubmit(payload);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey) {
      return;
    }
    if (!text.trim() || disabled) {
      return;
    }
    event.preventDefault();
    event.currentTarget.form?.requestSubmit();
  }

  function handleCityChange(nextCity: string) {
    setCityHint(nextCity);
    onCityChange?.(nextCity);
  }

  function handleDrop(event: DragEvent<HTMLFormElement>) {
    event.preventDefault();
    setIsDragging(false);
    const droppedFiles = Array.from(event.dataTransfer.files ?? []);
    if (droppedFiles.length) {
      setFiles((current) => [...current, ...droppedFiles]);
    }
    const droppedText = event.dataTransfer.getData("text/plain");
    if (droppedText) {
      if (/(https?:\/\/[^\s]+)/.test(droppedText)) {
        setLink(droppedText);
      } else {
        setText((current) => [current, droppedText].filter(Boolean).join("\n"));
      }
    }
  }

  return (
    <form
      aria-label="Agent 对话输入"
      className={`chat-composer ${isDragging ? "dragging" : ""}`}
      onDragLeave={() => setIsDragging(false)}
      onDragOver={(event) => {
        event.preventDefault();
        setIsDragging(true);
      }}
      onDrop={handleDrop}
      onSubmit={handleSubmit}
    >
      <h2 className="sr-only">Agent 对话输入</h2>
      {inputError ? <p role="alert">{inputError}</p> : null}
      <label className="sr-only-field">
        城市线索
        <select value={cityHint} onChange={(event) => handleCityChange(event.target.value)}>
          <option value="北京">北京</option>
          <option value="上海">上海</option>
          <option value="广州">广州</option>
          <option value="深圳">深圳</option>
        </select>
      </label>
      <label className="composer-text">
        <span className="sr-only">Agent 对话文本</span>
        <textarea
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="直接告诉 Agent 旅行需求，例如：帮我安排北京两天，轻松一点，想去故宫和胡同..."
          rows={3}
        />
      </label>
      <label className="sr-only-field">
        来源链接
        <input value={link} onChange={(event) => setLink(event.target.value)} placeholder="小红书/抖音/攻略链接" />
      </label>
      <div className="composer-toolbar">
        <span aria-live="polite" className="sr-only">
          {files.length ? `已拖入 ${files.length} 个文件` : link ? "已识别辅助链接" : "可拖入文件作为辅助素材"}
        </span>
        <button className="send-button" disabled={disabled} type="submit">
          <span className="sr-only">发送给 Agent</span>
          <span aria-hidden="true">&gt;</span>
        </button>
      </div>
    </form>
  );
}

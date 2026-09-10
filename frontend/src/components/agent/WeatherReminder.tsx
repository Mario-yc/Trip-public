import { useState } from "react";
import { WeatherReminderResponse } from "../../services/apiClient";

type WeatherReminderProps = {
  itineraryPlanId?: string;
  onCreate: (emailAddress: string, triggerDate: string) => Promise<WeatherReminderResponse | null>;
};

export function WeatherReminder({ itineraryPlanId, onCreate }: WeatherReminderProps) {
  const [emailAddress, setEmailAddress] = useState("");
  const [triggerDate, setTriggerDate] = useState(new Date().toISOString().slice(0, 10));
  const [result, setResult] = useState<WeatherReminderResponse | null>(null);
  const [errorMessage, setErrorMessage] = useState("");

  async function handleCreate() {
    setErrorMessage("");
    const response = await onCreate(emailAddress, triggerDate);
    if (response) {
      setResult(response);
    } else {
      setErrorMessage("请先生成行程后再创建提醒。");
    }
  }

  return (
    <section className="utility-panel" aria-label="Weather reminder">
      <h2>天气提醒</h2>
      <label>
        提醒邮箱
        <input
          value={emailAddress}
          onChange={(event) => setEmailAddress(event.target.value)}
          placeholder="demo@example.com"
          type="email"
        />
      </label>
      <label>
        触发日期
        <input value={triggerDate} onChange={(event) => setTriggerDate(event.target.value)} type="date" />
      </label>
      <button type="button" disabled={!itineraryPlanId} onClick={handleCreate}>
        创建模拟提醒
      </button>
      {errorMessage ? <p role="alert">{errorMessage}</p> : null}
      {result ? (
        <p>
          {result.simulatedStatus} · {result.providerName} · {result.subject}
        </p>
      ) : null}
    </section>
  );
}

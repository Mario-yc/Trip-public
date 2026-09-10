import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { AppShell } from "../../src/components/AppShell";
import { ExtractionReview } from "../../src/components/agent/ExtractionReview";
import { InspirationInput } from "../../src/components/agent/InspirationInput";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("renders inspiration input controls", () => {
  render(<InspirationInput onSubmit={() => undefined} />);

  expect(screen.getByText("Agent 对话输入")).toBeTruthy();
  expect(screen.getByLabelText("城市线索")).toBeTruthy();
  expect(screen.getByLabelText("Agent 对话文本")).toBeTruthy();
  expect(screen.queryByText("添加辅助素材")).toBeNull();
  expect(screen.queryByText(/纯文本将进入 Agent 主闭环/)).toBeNull();
});

test("uploads files before creating and extracting an inspiration", async () => {
  const fetchMock = vi.fn(async (url: RequestInfo | URL, _init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/source-materials/upload")) {
      return jsonResponse({
        sourceMaterialId: "mat_uploaded",
        kind: "screenshot",
        thumbnailUrl: "backend/data/uploads/thumbnails/mat_uploaded.thumb.txt",
        thumbnailPlaceholder: true,
        originalRetention: "temporary_cache",
        cacheStatus: "retained"
      });
    }
    if (path.endsWith("/inspirations")) {
      return jsonResponse({
        inspirationSetId: "insp_123",
        status: "extracting",
        sourceMaterialIds: ["mat_text", "mat_uploaded"]
      });
    }
    if (path.endsWith("/inspirations/insp_123/extract")) {
      return jsonResponse(extractionResponse());
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京 故宫博物院 拍照 预算 3000 元" }
  });
  const file = new File(["image"], "guide.png", { type: "image/png" });
  fireEvent.drop(screen.getByRole("form", { name: "Agent 对话输入" }), dataTransferWithFiles([file]));
  fireEvent.click(screen.getByText("发送给 Agent"));

  await waitFor(() => expect(screen.getByText("整体置信度：72%")).toBeTruthy());
  expect(screen.getByText("故宫博物院 游览")).toBeTruthy();

  const calledPaths = fetchMock.mock.calls.map((call) => String(call[0]));
  expect(calledPaths.some((path) => path.endsWith("/source-materials/upload"))).toBe(true);
  expect(calledPaths.some((path) => path.endsWith("/inspirations"))).toBe(true);
  expect(calledPaths.some((path) => path.endsWith("/inspirations/insp_123/extract"))).toBe(true);
  const createRequest = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/inspirations"));
  expect(JSON.parse(String(createRequest?.[1]?.body))).toMatchObject({
    socialLinks: [], sourceMaterialIds: ["mat_uploaded"]
  });
});

test("upload failure shows an error and does not create or extract", async () => {
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/source-materials/upload")) {
      return jsonResponse({ detail: "Unsupported file type. Supported image types: image/jpeg, image/png, image/webp." }, 400);
    }
    return jsonResponse({}, 500);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  const file = new File(["text"], "notes.txt", { type: "text/plain" });
  fireEvent.drop(screen.getByRole("form", { name: "Agent 对话输入" }), dataTransferWithFiles([file]));
  fireEvent.click(screen.getByText("发送给 Agent"));

  await waitFor(() =>
    expect(screen.getByText("Unsupported file type. Supported image types: image/jpeg, image/png, image/webp.")).toBeTruthy()
  );
  expect(screen.queryByText("正在让 Agent 规划行程...")).toBeNull();

  const calledPaths = fetchMock.mock.calls.map((call) => String(call[0]));
  expect(calledPaths.some((path) => path.endsWith("/source-materials/upload"))).toBe(true);
  expect(calledPaths.some((path) => path.endsWith("/inspirations"))).toBe(false);
  expect(calledPaths.some((path) => path.includes("/extract"))).toBe(false);
});

test("extraction review shows confidence, sources, and confirmation state", () => {
  render(
    <ExtractionReview
      budgetClues={["预算 3000 元"]}
      cityCandidates={["北京"]}
      confidence={0.72}
      fallbackUsed={true}
      needsUserConfirmation={true}
      poiCandidates={[{ name: "故宫博物院", confidence: 0.82, sourceLinks: ["https://example.com/guide"] }]}
      providerFailureReason="Default vision provider is not configured."
      providerName="mock-vision-provider"
      routeClues={["待用户确认路线顺序"]}
      sourceLinks={["https://example.com/guide"]}
      styleTags={["拍照优先"]}
      userVisibleCaveat="默认视觉 provider 不可用，已使用 mock 识别结果继续生成行程草案。"
    />
  );

  expect(screen.getByText("整体置信度：72%")).toBeTruthy();
  expect(screen.getByText("需要用户确认识别结果")).toBeTruthy();
  expect(screen.getByText("来源链接（1）")).toBeTruthy();
  expect(screen.getByText("默认视觉 provider 不可用，已使用 mock 识别结果继续生成行程草案。")).toBeTruthy();
});

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}

function dataTransferWithFiles(files: File[]) {
  return {
    dataTransfer: {
      files,
      getData: () => ""
    }
  };
}

function extractionResponse() {
  return {
    inspirationSetId: "insp_123",
    cityCandidates: ["北京"],
    poiCandidates: [{ name: "故宫博物院", confidence: 0.82, sourceLinks: ["https://example.com/guide"] }],
    styleTags: ["拍照优先"],
    budgetClues: ["预算 3000 元"],
    routeClues: ["待用户确认路线顺序"],
    confidence: 0.72,
    needsUserConfirmation: false,
    sourceLinks: ["https://example.com/guide"],
    providerName: "mock-vision-provider",
    fallbackUsed: false,
    itineraryDraft: {
      title: "北京灵感行程草案",
      editable: true,
      days: [
        {
          dayNumber: 1,
          title: "Day 1 初版可编辑行程",
          segments: [
            {
              id: "seg_1",
              title: "故宫博物院 游览",
              poiName: "故宫博物院",
              startTime: "09:30",
              durationMinutes: 120,
              transportMode: "公共交通/步行待确认",
              costItems: [
                {
                  label: "景点门票或预约费用",
                  amountCny: 0,
                  currency: "CNY",
                  isEstimate: true
                }
              ],
              reservationNotes: ["门票/预约状态待票务查询确认"]
            }
          ]
        }
      ]
    }
  };
}

import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";

import { TravelGuideAdviceCard } from "../../src/components/agent/TravelGuideAdviceCard";
import type { TravelGuideAdvice } from "../../src/services/apiClient";

afterEach(cleanup);

test("renders every matched guide as a full-width structured card with complete source summaries", () => {
  const longSummary = `${"北京高校、公园与美食路线建议。".repeat(28)} GUIDE_TAIL_987`;
  const advice: TravelGuideAdvice = {
    status: "completed",
    failureReason: null,
    recommendations: [
      {
        title: "北京高校、公园和美食两日游攻略",
        text: longSummary,
        sourceUrl: "https://travel.example/beijing-guide",
        sourceName: "示例旅行网",
        queriedAt: "2026-09-01T12:00:00Z",
        credibilityRank: "guide",
        summaryKind: "search_result_snippet",
        poiVerificationStatus: "unverified_advice"
      },
      {
        title: "北京高校访客指南",
        text: "参观前核对各校访客预约规则。",
        sourceUrl: "https://travel.example/campus-guide",
        poiVerificationStatus: "unverified_advice"
      }
    ],
    cautions: [
      {
        text: "热门日期可能需要提前预约。",
        sourceUrl: "https://travel.example/beijing-guide"
      }
    ],
    sourceRefs: [
      {
        title: "北京高校、公园和美食两日游攻略",
        url: "https://travel.example/beijing-guide",
        sourceName: "示例旅行网",
        queriedAt: "2026-09-01T12:00:00Z",
        credibilityRank: "guide"
      }
    ],
    queryFingerprint: "fingerprint",
    queriedAt: "2026-09-01T12:00:00Z",
    caveat: "以下为搜索结果摘要，不是网页全文。",
    queryCount: 1,
    relevanceFilter: {
      acceptedResultCount: 2,
      rejectedResultCount: 1,
      reasonCounts: { theme_mismatch: 1 }
    }
  };

  render(<TravelGuideAdviceCard advice={advice} />);

  const card = screen.getByRole("region", { name: "普通攻略建议" });
  expect(card.classList.contains("agent-guide-advice")).toBe(true);
  expect(within(card).getByText(/GUIDE_TAIL_987/)).toBeTruthy();
  expect(within(card).getByText("参观前核对各校访客预约规则。")).toBeTruthy();
  expect(within(card).getByText("热门日期可能需要提前预约。")).toBeTruthy();
  const originalLinks = within(card).getAllByRole("link", { name: "打开原文" });
  expect(originalLinks).toHaveLength(2);
  expect(originalLinks[0].getAttribute("href")).toBe("https://travel.example/beijing-guide");
  expect(originalLinks[0].getAttribute("target")).toBe("_blank");
  expect(within(card).getByText("搜索摘要，不是网页全文")).toBeTruthy();
});

test("distinguishes provider failure from a successful but irrelevant result set", () => {
  const advice: TravelGuideAdvice = {
    status: "failed",
    failureReason: "all_web_search_providers_failed_or_empty",
    recommendations: [],
    cautions: [],
    sourceRefs: [],
    queryFingerprint: "fingerprint",
    queriedAt: "2026-09-01T12:00:00Z",
    caveat: "普通攻略只作经验性建议。",
    queryCount: 2,
    attemptedProviders: ["bing-html-search"],
    relevanceFilter: {
      acceptedResultCount: 0,
      rejectedResultCount: 0,
      reasonCounts: {}
    }
  };

  render(<TravelGuideAdviceCard advice={advice} />);

  expect(screen.getByText("攻略来源暂时不可用")).toBeTruthy();
  expect(screen.getByText(/已尝试 1 个搜索来源/)).toBeTruthy();
});

test("shows the evidence-bound conclusion before the collapsed raw summaries", () => {
  const advice: TravelGuideAdvice = {
    status: "completed",
    recommendations: [
      {
        refId: "guide_ref_1",
        title: "北京高校参观攻略",
        text: "参观前需要提前预约。",
        sourceUrl: "https://travel.example/campus",
        poiVerificationStatus: "unverified_advice"
      }
    ],
    cautions: [],
    sourceRefs: [{ refId: "guide_ref_1", title: "北京高校参观攻略", url: "https://travel.example/campus" }],
    queryFingerprint: "query",
    evidenceFingerprint: "evidence",
    queriedAt: "2026-09-01T12:00:00Z",
    caveat: "只作经验性建议。",
    queryCount: 1,
    relevanceFilter: { acceptedResultCount: 1, rejectedResultCount: 0, reasonCounts: {} },
    conclusion: {
      status: "ready",
      overview: "高校参观应先核对预约规则。",
      takeaways: [
        {
          intentType: "campus_visit",
          themeLabel: "高校参观",
          text: "出发前核对访客预约规则。",
          sourceRefIds: ["guide_ref_1"]
        }
      ],
      conflicts: [],
      missingThemes: [],
      evidenceBasis: "search_result_snippets",
      generationMethod: "deepseek_structured_v1"
    }
  };

  render(<TravelGuideAdviceCard advice={advice} />);

  expect(screen.getByRole("region", { name: "攻略整理结论" })).toBeTruthy();
  expect(screen.getByText("高校参观应先核对预约规则。")).toBeTruthy();
  expect(screen.getByText("依据：北京高校参观攻略")).toBeTruthy();
  expect(screen.getByText("来源摘要与原文（1）")).toBeTruthy();
});

test("hides pre-filter historical summaries and never renders their unsafe source protocols", () => {
  const advice: TravelGuideAdvice = {
    status: "completed",
    recommendations: [
      {
        title: "历史攻略摘要",
        text: "这段历史摘要仍应可读。",
        sourceUrl: "javascript:alert(document.domain)",
        poiVerificationStatus: "unverified_advice"
      }
    ],
    cautions: [
      {
        text: "历史注意事项仍应可读。",
        sourceUrl: "data:text/html,unsafe"
      }
    ],
    sourceRefs: [
      {
        title: "不安全历史来源",
        url: "http://127.0.0.1/private"
      }
    ],
    queryFingerprint: "legacy",
    queriedAt: "",
    caveat: "普通攻略只作经验性建议。",
    queryCount: 1
  };

  render(<TravelGuideAdviceCard advice={advice} />);

  expect(screen.getByText("历史攻略结果待重新核验")).toBeTruthy();
  expect(screen.getByText("旧摘要已隐藏")).toBeTruthy();
  expect(screen.queryByText("这段历史摘要仍应可读。")).toBeNull();
  expect(screen.queryByText("历史注意事项仍应可读。")).toBeNull();
  expect(screen.queryByRole("link")).toBeNull();
});

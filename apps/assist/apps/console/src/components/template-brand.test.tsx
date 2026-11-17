// @vitest-environment jsdom

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import neutral from "@/config/template.json";

vi.mock("next/image", () => ({
  default: ({ src, alt, width, height }: { src: string; alt: string; width: number; height: number }) =>
    React.createElement("img", { src, alt, width, height }),
}));
vi.mock("next/font/google", () => ({ Readex_Pro: () => ({ variable: "test-font" }) }));
vi.mock("@/components/theme-provider", () => ({ ThemeProvider: ({ children }: { children: React.ReactNode }) => children }));
const originalScrollIntoView = Element.prototype.scrollIntoView;

beforeEach(() => {
  vi.resetModules();
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("Unexpected request")));
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.doUnmock("@/config/template.json");
  vi.doUnmock("@/components/agent");
  vi.doUnmock("@/lib/signalr-service");
  vi.doUnmock("@/lib/use-operator-channel");
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  Element.prototype.scrollIntoView = originalScrollIntoView;
});

describe("public Assist presentation", () => {
  it("preserves panel resizing when the configured locale uses RTL", async () => {
    const config = structuredClone(neutral);
    config.locale.default = "ar";
    config.features.agentAssist = true;
    vi.doMock("@/config/template.json", () => ({ default: config }));
    vi.doMock("@/components/agent", () => ({
      ConversationPanel: () => null,
      InsightsPanel: () => null,
      KnowledgeChatPanel: () => null,
      SessionListPanel: () => null,
    }));
    vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(1000);
    const { default: AgentPage } = await import("@/app/agent/page");
    const { container } = render(<AgentPage />);
    const firstPanel = container.querySelector<HTMLElement>('[style*="min-width"]')!;
    const width = Number.parseFloat(firstPanel.style.width);
    fireEvent.mouseDown(screen.getAllByTitle("Drag to resize")[0], { clientX: 100 });
    fireEvent.mouseMove(document, { clientX: 110 });
    fireEvent.mouseUp(document);
    expect(Number.parseFloat(firstPanel.style.width)).toBe(width - 10);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("does not mount telemetry, polling or operator subscriptions when agentAssist is unselected", async () => {
    const startConnection = vi.fn();
    const onNewMessage = vi.fn();
    const onInsightsUpdated = vi.fn();
    const useOperatorChannel = vi.fn();
    vi.doMock("@/lib/signalr-service", () => ({
      startConnection, onNewMessage, onInsightsUpdated,
      joinConversation: vi.fn(), leaveConversation: vi.fn(), isConnected: () => false,
    }));
    vi.doMock("@/lib/use-operator-channel", () => ({ useOperatorChannel }));
    const intervals = vi.spyOn(globalThis, "setInterval");
    const { ConversationPanel, InsightsPanel, SessionListPanel } = await import("@/components/agent");
    const { container } = render(<>
      <ConversationPanel conversationId="saved-session" />
      <InsightsPanel conversationId="saved-session" />
      <SessionListPanel selectedSessionId="saved-session" onSessionSelect={vi.fn()} />
    </>);
    expect(container.textContent).toBe("");
    expect(fetch).not.toHaveBeenCalled();
    expect(startConnection).not.toHaveBeenCalled();
    expect(onNewMessage).not.toHaveBeenCalled();
    expect(onInsightsUpdated).not.toHaveBeenCalled();
    expect(useOperatorChannel).not.toHaveBeenCalled();
    expect(intervals).not.toHaveBeenCalled();
    cleanup();

    const { default: AgentPage } = await import("@/app/agent/page");
    const { default: SupervisorPage } = await import("@/app/supervisor/page");
    const { default: Home } = await import("@/app/page");
    render(<><AgentPage /><SupervisorPage /><Home /></>);
    expect(screen.getAllByText("Agent assist is not enabled.")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Agent Console" }).hasAttribute("disabled")).toBe(true);
    expect(fetch).not.toHaveBeenCalled();
    expect(startConnection).not.toHaveBeenCalled();
    expect(intervals).not.toHaveBeenCalled();
  });

  it("loads real session API data only when agentAssist is selected", async () => {
    const config = structuredClone(neutral);
    config.features.agentAssist = true;
    vi.doMock("@/config/template.json", () => ({ default: config }));
    vi.mocked(fetch).mockResolvedValue({ ok: true, json: async () => ({ sessions: [] }) } as Response);
    const { SessionListPanel } = await import("./agent/session-list-panel");
    render(<SessionListPanel onSessionSelect={vi.fn()} />);
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/sessions"));
  });

  for (const extension of ["png", "jpg", "webp"]) {
    it(`accepts a prepared same-origin ${extension} logo without a private source path`, async () => {
      const config = {
        ...structuredClone(neutral),
        brand: { ...neutral.brand, logoUrl: `/brand-logo.${extension}` },
      };
      vi.doMock("@/config/template.json", () => ({ default: config }));
      const { TemplateBrand } = await import("./template-brand");
      const layout = await import("@/app/layout");
      render(<TemplateBrand />);
      expect(screen.getByRole("img", { name: "Contact center" }).getAttribute("src")).toBe(config.brand.logoUrl);
      expect(layout.metadata.icons).toEqual({ icon: config.brand.logoUrl });
      expect(Object.hasOwn(config.brand, "logoFile")).toBe(false);
    });
  }

  it("ships neutral branding with no logo or bundled suggested questions", async () => {
    const { TemplateBrand } = await import("./template-brand");
    const { KnowledgeChatPanel } = await import("./agent/knowledge-chat-panel");
    render(<><TemplateBrand /><KnowledgeChatPanel /></>);
    expect(screen.getByText("Contact center")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Assistant" })).toBeTruthy();
    expect(screen.getByText("How can we help?")).toBeTruthy();
    expect(screen.queryAllByRole("img")).toHaveLength(0);
    expect(screen.queryByText("What's our refund policy?")).toBeNull();
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("uses configured branding, locale and quick-action drafts without changing the chat payload", async () => {
    const config = {
      ...structuredClone(neutral),
      brand: {
        ...neutral.brand,
        name: { en: "Configured workspace", ar: "مساحة العمل" },
        assistantName: { en: "Configured assistant", ar: "مساعد الخدمة" },
        logoUrl: "/generic-test.svg",
        colors: { primary: "#123456", accent: "#abcdef" },
      },
      locale: { ...neutral.locale, default: "ar" },
      welcome: { title: { en: "Welcome", ar: "مرحباً بك" }, description: { en: "", ar: "اختر قناة." } },
      quickActions: [{ id: "help", label: { en: "Get help", ar: "طلب المساعدة" }, prompt: { en: "Please help.", ar: "أحتاج المساعدة." } }],
    };
    vi.doMock("@/config/template.json", () => ({ default: config }));
    const { TemplateBrand } = await import("./template-brand");
    const { KnowledgeChatPanel } = await import("./agent/knowledge-chat-panel");
    const layout = await import("@/app/layout");
    const markup = renderToStaticMarkup(<layout.default><TemplateBrand /></layout.default>);
    expect(markup).toContain('lang="ar" dir="rtl"');
    expect(markup).toContain("--brand-primary:#123456");
    expect(markup).toContain("--brand-accent:#abcdef");
    expect(layout.metadata.title).toBe("مساحة العمل | مساعدة الموظف");

    vi.mocked(fetch).mockResolvedValue({ ok: true, json: async () => ({ answer: "A test response.", citations: [] }) } as Response);
    const { container } = render(<><TemplateBrand /><KnowledgeChatPanel conversationId="test-session" /></>);
    expect(screen.getByRole("img", { name: "مساحة العمل" }).getAttribute("src")).toBe("/generic-test.svg");
    expect(screen.getByRole("heading", { name: "مساعد الخدمة" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "طلب المساعدة" }));
    expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe("أحتاج المساعدة.");
    expect(fetch).not.toHaveBeenCalled();
    fireEvent.submit(container.querySelector("form")!);
    await waitFor(() => expect(screen.getByText("A test response.")).toBeTruthy());
    expect(fetch).toHaveBeenCalledWith("/api/agent-assist/chat", expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ question: "أحتاج المساعدة.", conversationId: "test-session" }),
    }));
  });
});

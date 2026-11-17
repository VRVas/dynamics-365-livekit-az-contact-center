import Link from "next/link";
import { TemplateBrand } from "@/components/template-brand";
import { assistTitle, template, text } from "@/config/template";

export default function Home() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-8 p-8">
      <div className="flex flex-col items-center text-center">
        <TemplateBrand />
        <h1 className="text-4xl font-bold tracking-tight mt-4">{assistTitle}</h1>
        <h2 className="mt-4 text-xl">{text(template.welcome.title)}</h2>
        {text(template.welcome.description) && <p className="mt-2 text-muted-foreground">{text(template.welcome.description)}</p>}
      </div>

      <div className="flex gap-4">
        {template.features.agentAssist ? <Link
          href="/agent"
          className="inline-flex h-10 items-center justify-center rounded-md bg-primary px-8 text-sm font-medium text-primary-foreground ring-offset-background transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          Agent Console
        </Link> : <button type="button" disabled aria-disabled="true" title="Not enabled"
          className="inline-flex h-10 cursor-not-allowed items-center justify-center rounded-md border px-8 text-sm opacity-60">
          Agent Console
        </button>}
        <button
          type="button"
          disabled
          title={template.features.agentAssist ? "Coming soon" : "Not enabled"}
          aria-disabled="true"
          className="inline-flex h-10 cursor-not-allowed items-center justify-center gap-2 rounded-md border border-input bg-background px-8 text-sm font-medium opacity-60"
        >
          Supervisor Dashboard
          <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-600">
            {template.features.agentAssist ? "Coming soon" : "Not enabled"}
          </span>
        </button>
      </div>
    </div>
  );
}

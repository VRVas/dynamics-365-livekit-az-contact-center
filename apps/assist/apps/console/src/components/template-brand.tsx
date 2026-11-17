import Image from "next/image";
import { assistTitle, locale, template, text } from "@/config/template";

export function TemplateBrand() {
  return (
    <div className="flex min-w-0 items-center gap-3">
      {template.brand.logoUrl && (
        <Image src={template.brand.logoUrl} alt={text(template.brand.name)}
          width={146} height={46} unoptimized className="h-8 w-auto object-contain" />
      )}
      <span className="font-semibold">{text(template.brand.name)}</span>
    </div>
  );
}

export function AssistUnavailable() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-5 p-6 text-center">
      <TemplateBrand />
      <h1 className="text-xl font-semibold">{assistTitle}</h1>
      <p role="status">{locale === "ar" ? "مساعدة الموظف غير مفعّلة." : "Agent assist is not enabled."}</p>
    </main>
  );
}

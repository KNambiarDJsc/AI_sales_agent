import { Suspense } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";

// The console is client-rendered (it needs the login token from the browser), so the
// shell reads the URL at runtime under Suspense.
export default function ConsoleLayout({ children }: { children: React.ReactNode }) {
  return (
    <Suspense fallback={null}>
      <ConsoleShell>{children}</ConsoleShell>
    </Suspense>
  );
}

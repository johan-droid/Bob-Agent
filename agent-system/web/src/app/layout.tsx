import type { Metadata } from "next";
import "./globals.css";
import { Sidebar } from "@/components/Sidebar";
import { Header } from "@/components/Header";
import { ToastProvider } from "@/components/Toast";
import { CommandPalette } from "@/components/CommandPalette";

export const metadata: Metadata = {
  title: "Bob Agent System",
  description: "Autonomous agentic workspace and chat interface",
};

// Runs before first paint: cookie (persisted choice) wins, otherwise the OS
// prefers-color-scheme default. Keeps server markup and client in sync.
const THEME_SCRIPT = `(function(){try{var m=document.cookie.match(/(?:^|; )bob-theme=(light|dark)/);var t=m?m[1]:(window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');document.documentElement.setAttribute('data-theme',t);}catch(e){}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body>
        <ToastProvider>
          <div className="app-shell">
            <Sidebar />
            <main className="main-content">
              <Header />
              {children}
            </main>
          </div>
          <CommandPalette />
        </ToastProvider>
      </body>
    </html>
  );
}

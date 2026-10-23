import type { Metadata } from "next";
import { Inter, Space_Grotesk } from "next/font/google";
import "./globals.css";

const inter = Inter({ variable: "--font-inter", subsets: ["latin"] });
const spaceGrotesk = Space_Grotesk({ variable: "--font-space-grotesk", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Dialwave · AI Sales Caller",
  description: "Upload leads, let the AI agent call them one by one, and get the interested ones back.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${inter.variable} ${spaceGrotesk.variable}`}>
      <body className="min-h-screen" suppressHydrationWarning>
        <div className="aurora">
          <span />
        </div>
        <div className="grid-overlay" />
        {children}
      </body>
    </html>
  );
}

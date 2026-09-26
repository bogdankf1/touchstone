import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Touchstone | Runs',
  description: 'Local measurement preview for simulated workflows',
};
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

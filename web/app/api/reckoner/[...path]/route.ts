import { NextRequest, NextResponse } from 'next/server';
const routes: Record<string, string[]> = {
  cases: ['GET'],
  case: ['GET'],
  reviews: ['POST'],
  configurations: ['GET', 'POST'],
  'configurations/activate': ['POST'],
  'configurations/preview': ['GET'],
};
async function proxy(request: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  const path = (await params).path.join('/');
  if (!routes[path]?.includes(request.method))
    return NextResponse.json({ detail: 'Unknown operation' }, { status: 404 });
  if (
    request.method === 'POST' &&
    request.headers.get('origin') &&
    new URL(request.headers.get('origin')!).host !== request.headers.get('host')
  )
    return NextResponse.json({ detail: 'Invalid origin' }, { status: 403 });
  try {
    const base = process.env.RECKONER_API_URL;
    if (!base) return NextResponse.json({ detail: 'Reckoner API unavailable' }, { status: 503 });
    const url = new URL(`/v1/${path}`, base);
    url.search = request.nextUrl.search;
    const body = request.method === 'POST' ? await request.text() : undefined;
    if (body && body.length > 65536)
      return NextResponse.json({ detail: 'Request too large' }, { status: 413 });
    const response = await fetch(url, {
      method: request.method,
      cache: 'no-store',
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body,
      signal: AbortSignal.timeout(15000),
    });
    if (!response.ok)
      return NextResponse.json(
        { detail: 'Reckoner request could not be completed' },
        { status: [404, 409, 422, 503].includes(response.status) ? response.status : 502 },
      );
    return NextResponse.json(await response.json());
  } catch {
    return NextResponse.json({ detail: 'Reckoner API unavailable' }, { status: 503 });
  }
}
export const GET = proxy;
export const POST = proxy;

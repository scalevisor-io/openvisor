// Prerendered robots.txt: the sitemap URL follows the configured site (SITE_URL
// build arg), completing the white-label story - a rebranded spoke advertises
// its own sitemap, never another deployment's.
import type { APIRoute } from 'astro';

export const GET: APIRoute = ({ site }) => {
  const base = (site ?? new URL('https://openvisor.example.com')).toString().replace(/\/$/, '');
  const body = `User-agent: *\nAllow: /\n\nSitemap: ${base}/sitemap-index.xml\n`;
  return new Response(body, { headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
};

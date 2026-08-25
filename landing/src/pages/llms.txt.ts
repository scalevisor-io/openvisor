// /llms.txt, generated from the shared content system so it can never drift
// from the pages. The body (brand + consultant already substituted) lives in
// src/data/site.example.yml with the app links already substituted; the site
// URL is filled here from Astro's `site` so it tracks SITE_URL.
import type { APIRoute } from 'astro';
import content from '../lib/content';

export const prerender = true;

export const GET: APIRoute = ({ site }) => {
  const siteUrl = (site?.href ?? 'https://openvisor.example.com').replace(/\/$/, '');
  const body = content.llms.replaceAll('{{SITE_URL}}', siteUrl);
  return new Response(body, {
    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
  });
};

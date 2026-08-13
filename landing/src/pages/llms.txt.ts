// /llms.txt, generated from the shared content system so it can never drift
// from the pages. The body (brand + consultant already substituted) lives in
// src/data/site.example.yml; the platform URLs are filled from build-time env
// here so they track SITE_URL / APP_URL.
import type { APIRoute } from 'astro';
import content from '../lib/content';
import { appUrl } from '../lib/site';

export const prerender = true;

export const GET: APIRoute = ({ site }) => {
  const siteUrl = (site?.href ?? 'https://openvisor.example.com').replace(/\/$/, '');
  const body = content.llms
    .replaceAll('{{SITE_URL}}', siteUrl)
    .replaceAll('{{APP_URL}}', appUrl);
  return new Response(body, {
    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
  });
};

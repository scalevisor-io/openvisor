// Build-time content loader for the white-label landing. Reads the committed
// site.example.yml (neutral template copy) or a gitignored site.yml override when
// present (wholesale replacement, no merge), applies {{BRAND_NAME}} /
// {{CONSULTANT_NAME}} substitution from build-time env plus the {{APP_URL}} /
// {{MCP_URL}} / {{TOKENS_URL}} app links, and returns a typed, validated
// SiteContent. Node-only: it touches the filesystem, so it must never
// be imported into client-side code. Missing required keys fail the build loudly.
import fs from 'node:fs';
import path from 'node:path';
import yaml from 'js-yaml';
import {
  aiIntakeUrl,
  appUrl,
  brandColorPrimary,
  brandColorSecondary,
  brandName,
  consultantName,
  mcpUrl,
  programsUrl,
  signupUrl,
  tokensUrl,
} from './site';

export interface Offer {
  name: string;
  priceCurrency: string;
  description: string;
}

export interface Phase {
  chip: { label: string; tone: string; indicator: string };
  stages: string[];
  events: { icon: string; body: string; meta: string }[];
}

export interface FeatureRequest {
  icon: string;
  body: string;
  meta: string;
}

export interface PlatformPoint {
  icon: string;
  title: string;
  surface: string;
  caption: string;
}

export interface TrustPoint {
  icon: string;
  strong: string;
  text: string;
}

export interface Step {
  n: string;
  title: string;
  body: string;
  who?: string;
}

export interface ProgramLine {
  g: string;
  text: string;
  tone?: string;
}

export interface ProgramExample {
  monogram: string;
  from: string;
  to: string;
  title: string;
  mode: string;
  body: string;
  meta: string;
  run: { id: string; lines: ProgramLine[] };
}

export interface WayCard {
  tag: string;
  title: string;
  lede: string;
  list: string[];
  forLabel: string;
  forText: string;
  cta: string;
}

// One door onto the Curated AI path (the web app, or the customer's own coding
// agent over MCP). `surface` is a mono product strip in HTML using the same
// `ps-*` classes as projects.points; `list` items may carry inline <code>.
export interface ChannelCard extends WayCard {
  surface: string;
}

export interface PriceCard {
  tag: string;
  title: string;
  body: string;
}

// §profile: the consultant-led sections (hero, pillars, services, proof, about).
// Everything here is instance content - a spoke rewrites it in site.yml.
export interface Pillar {
  icon: string;
  title: string;
  body: string;
}

// The door a service opens onto the platform: the Curated AI path, the
// hand-built Direct quote, or a ready-made Program (§28).
export type ServiceDoor = 'curated' | 'direct' | 'program';

export interface ServiceItem {
  icon: string;
  title: string;
  summary: string;
  capabilities: string[];
  door: ServiceDoor;
}

export interface Stat {
  value: string;
  label: string;
}

export interface FeaturedItem {
  kind: string;
  title: string;
  href: string;
  image?: string;
  lang?: string;
}

export interface SocialLink {
  label: string;
  href: string;
  icon: string;
}

export interface Profile {
  hero: {
    headline: string;
    accent?: string;
    roles: string[];
    lede: string;
    ctaPrimary: string;
    ctaPrimaryHref: string;
    ctaSecondary?: string;
    ctaSecondaryHref?: string;
    availability?: string;
    // A committed portrait under public/ (a fork asset). The admin-uploaded
    // photo (§consultant photo) replaces it at runtime when the API serves one.
    portrait?: string;
  };
  pillars: { eyebrow: string; title: string; items: Pillar[] };
  services: {
    eyebrow: string;
    title: string;
    intro: string;
    doorLabels: Record<ServiceDoor, string>;
    items: ServiceItem[];
  };
  proof: {
    eyebrow: string;
    title: string;
    intro: string;
    stats: Stat[];
    featured: FeaturedItem[];
  };
  about: { eyebrow: string; title: string; paragraphs: string[] };
  socials: SocialLink[];
}

// §theme: the white-label style surface. Every key is optional - an unset key
// keeps the stock token from global.css. Colours are CSS colour strings.
export interface ThemePalette {
  bg?: string;
  bgElevated?: string;
  border?: string;
  text?: string;
  textMuted?: string;
  textStrong?: string;
  accent?: string;
  gradientFrom?: string;
  gradientTo?: string;
}

export interface Theme {
  // dark: dark only (stock). light: light only. toggle: dark default + a
  // header switch remembered per visitor.
  mode?: 'dark' | 'light' | 'toggle';
  fonts?: { brand?: string; body?: string; mono?: string };
  colors?: ThemePalette;
  light?: ThemePalette;
  radius?: string;
  ornaments?: { ticks?: boolean; grid?: boolean };
}

// Section ids the home page can render, in the order site.yml lists them.
export const SECTION_IDS = [
  'hero',
  'specs',
  'pillars',
  'services',
  'proof',
  'channels',
  'platform',
  'programs',
  'how',
  'pricing',
  'sovereign',
  'about',
  'finalCta',
] as const;
export type SectionId = (typeof SECTION_IDS)[number];
export const DEFAULT_SECTIONS: SectionId[] = [...SECTION_IDS];

export interface SiteContent {
  seo: {
    title: string;
    description: string;
    themeColor: string;
    ogSiteName: string;
    orgName: string;
    orgDescription: string;
    sameAs: string[];
    serviceType: string;
    serviceDescription: string;
    serviceAreaServed: string;
    offers: Offer[];
  };
  legal: {
    entity: string;
    // Optional: the operating company's registered address. The admin Settings
    // page overrides both this and `entity` at runtime (see layouts/Base.astro).
    address?: string;
    consultant: string;
    contactEmail: string;
    privacyUpdated: string;
    termsUpdated: string;
  };
  footer: {
    tagline: string;
  };
  profile: Profile;
  theme?: Theme;
  sections?: SectionId[];
  specs: string[];
  stageLabels: string[];
  phases: Phase[];
  featureRequests: FeatureRequest[];
  // The ways-to-work section: the Curated AI path's two doors side by side and
  // the hand-built Direct quote as a full-width card under them.
  channels: {
    eyebrow: string;
    title: string;
    intro?: string;
    platform: ChannelCard;
    agent: ChannelCard;
    quote: WayCard;
    note: string;
  };
  projects: {
    eyebrow: string;
    title: string;
    intro: string;
    visualName: string;
    visualCaption: string;
    points: PlatformPoint[];
  };
  programs: {
    eyebrow: string;
    title: string;
    intro: string;
    examples: ProgramExample[];
    cta: string;
  };
  how: {
    eyebrow: string;
    title: string;
    intro: string;
    steps: Step[];
  };
  pricing: {
    eyebrow: string;
    title: string;
    intro: string;
    primary: PriceCard;
    secondary: PriceCard;
    cta: string;
  };
  sovereign: {
    eyebrow: string;
    title: string;
    intro: string;
    trustPoints: TrustPoint[];
  };
  finalCta: {
    titleLead: string;
    titleAccent: string;
    lede: string;
    ctaPrimary: string;
    ctaSecondaryLabel: string;
    ctaSecondaryHref: string;
  };
  // The /llms.txt body. Brand, consultant and app links are already
  // substituted; the {{SITE_URL}} marker is filled by the llms.txt endpoint.
  llms: string;
}

// Anchored to the project root (cwd under both `astro dev` and `astro build`)
// rather than import.meta.url, which Vite rewrites to the bundled chunk path in
// the static build - the YAML is not co-located with it there.
const dataDir = path.join(process.cwd(), 'src', 'data');
const overridePath = path.join(dataDir, 'site.yml');
const examplePath = path.join(dataDir, 'site.example.yml');
const sourcePath = fs.existsSync(overridePath) ? overridePath : examplePath;

let raw = fs.readFileSync(sourcePath, 'utf8');
raw = raw
  .replaceAll('{{BRAND_NAME}}', brandName)
  .replaceAll('{{CONSULTANT_NAME}}', consultantName)
  .replaceAll('{{APP_URL}}', appUrl)
  .replaceAll('{{MCP_URL}}', mcpUrl)
  .replaceAll('{{TOKENS_URL}}', tokensUrl)
  .replaceAll('{{SIGNUP_URL}}', signupUrl)
  .replaceAll('{{AI_INTAKE_URL}}', aiIntakeUrl)
  .replaceAll('{{PROGRAMS_URL}}', programsUrl)
  .replaceAll('{{BRAND_COLOR_PRIMARY}}', brandColorPrimary)
  .replaceAll('{{BRAND_COLOR_SECONDARY}}', brandColorSecondary);

const data = yaml.load(raw) as SiteContent;

// Fail the build loudly if the YAML is missing a top-level section, so a broken
// or half-written site.yml never ships a silently empty page.
const required: (keyof SiteContent)[] = [
  'seo',
  'legal',
  'footer',
  'profile',
  'specs',
  'stageLabels',
  'phases',
  'featureRequests',
  'channels',
  'projects',
  'programs',
  'how',
  'pricing',
  'sovereign',
  'finalCta',
  'llms',
];
if (!data || typeof data !== 'object') {
  throw new Error(`content: ${sourcePath} did not parse to an object`);
}
const missing = required.filter((key) => data[key] == null);
if (missing.length) {
  throw new Error(`content: ${sourcePath} is missing required keys: ${missing.join(', ')}`);
}

// The section order is content too: an unknown id fails the build rather than
// silently dropping a section a spoke expected to see.
const unknown = (data.sections ?? []).filter((id) => !SECTION_IDS.includes(id));
if (unknown.length) {
  throw new Error(`content: ${sourcePath} lists unknown sections: ${unknown.join(', ')}`);
}
export const sections: SectionId[] = data.sections?.length ? data.sections : DEFAULT_SECTIONS;
export const hasSection = (id: SectionId) => sections.includes(id);

export const site: SiteContent = data;
export default site;

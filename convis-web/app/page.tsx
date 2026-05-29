// Public marketing landing page served at the root of the webapp.
// The actual content lives in `public/landing.html` so the marketing team can
// edit it without touching the Next.js build. We read the file at build time
// (this is a server component), extract <style> and <body>, and inject both
// into the React tree. Metadata is mirrored from the HTML <head> so the page
// gets correct SEO/OG tags.
//
// Previous behaviour was `redirect('/register')`. Visitors who hit `/` now see
// the marketing page; the "Start free" CTAs in the page link to /register.
import fs from 'fs';
import path from 'path';
import type { Metadata } from 'next';

export const metadata: Metadata = {
  title: 'Convis AI — Build AI Voice & Chat Agents | No-Code Conversational AI Platform',
  description:
    'Convis AI lets you build and deploy intelligent voice and chat agents in under 15 minutes. Connect your knowledge base, integrate with Salesforce, Jira, HubSpot, and 50+ tools. Multilingual, agentic, and fully no-code.',
  keywords: [
    'AI voice agent',
    'conversational AI platform',
    'chatbot builder',
    'voice bot',
    'RAG knowledge base',
    'Salesforce integration',
    'no-code AI',
    'agentic AI',
    'multilingual chatbot',
    'AI agent platform',
  ],
  authors: [{ name: 'Convis AI' }],
  alternates: { canonical: 'https://convis.ai/' },
  openGraph: {
    title: 'Convis AI — Build AI Voice & Chat Agents in Minutes',
    description:
      'Deploy human-like voice and chat agents connected to your knowledge base and CRM. No code required. 30+ languages. Live in 15 minutes.',
    type: 'website',
    url: 'https://convis.ai/',
    siteName: 'Convis AI',
  },
  twitter: {
    card: 'summary_large_image',
    title: 'Convis AI — Build AI Voice & Chat Agents in Minutes',
    description:
      'Deploy human-like voice and chat agents connected to your knowledge base and CRM. No code required. 30+ languages. Live in 15 minutes.',
  },
  robots: { index: true, follow: true },
};

// Cache the parsed HTML across requests in dev — read once at module load.
const { styles, body } = (() => {
  const html = fs.readFileSync(
    path.join(process.cwd(), 'public', 'landing.html'),
    'utf8'
  );
  const styleMatch = html.match(/<style[^>]*>([\s\S]*?)<\/style>/);
  const bodyMatch = html.match(/<body[^>]*>([\s\S]*?)<\/body>/);
  return {
    styles: styleMatch ? styleMatch[1] : '',
    body: bodyMatch ? bodyMatch[1] : '',
  };
})();

// JSON-LD structured data — mirrors the original landing-page schema.
const jsonLd = {
  '@context': 'https://schema.org',
  '@type': 'SoftwareApplication',
  name: 'Convis AI',
  url: 'https://convis.ai',
  description:
    'Convis AI is a no-code platform for building and deploying intelligent voice and chat agents. Connect any knowledge base, integrate with CRMs and ticketing systems, and go live in under 15 minutes.',
  applicationCategory: 'BusinessApplication',
  operatingSystem: 'Web',
  offers: {
    '@type': 'Offer',
    priceCurrency: 'INR',
    price: '15',
    description: 'Starting at ₹15 per minute for self-service AI agent deployment',
  },
  featureList: [
    'AI Voice Agents',
    'AI Chat Agents',
    'RAG Knowledge Base',
    'CRM Integrations',
    '30+ Languages',
    'No-code builder',
    'Salesforce integration',
    'Jira integration',
    'HubSpot integration',
  ],
  contactPoint: {
    '@type': 'ContactPoint',
    email: 'hello@convis.ai',
    contactType: 'sales',
  },
};

export default function Home() {
  return (
    <>
      {/* Google Fonts used by the landing page (Outfit + Plus Jakarta Sans).
          Hoisted by React into <head>. */}
      <link rel="preconnect" href="https://fonts.googleapis.com" />
      <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
      <link
        href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800;900&family=Plus+Jakarta+Sans:ital,wght@0,300;0,400;0,500;0,600;1,300;1,400&display=swap"
        rel="stylesheet"
      />
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd) }}
      />
      <style dangerouslySetInnerHTML={{ __html: styles }} />
      {/* Defensive override: globals.css (loaded by RootLayout for the dashboard)
          sets body { background, color, font-family } at the same specificity.
          Without this, depending on which stylesheet React/Next hoists last,
          the landing could render with the dashboard's light theme. */}
      <style
        dangerouslySetInnerHTML={{
          __html:
            "html,body{background:#050810!important;color:#f0f4ff!important;font-family:'Plus Jakarta Sans',sans-serif!important;}",
        }}
      />
      <div dangerouslySetInnerHTML={{ __html: body }} />
    </>
  );
}

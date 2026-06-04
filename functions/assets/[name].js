const obsolete_assets = new Set([
  "index-CcYcEs2C.css",
  "index-CD4z4gjT.js",
  "index-CUYf0Vs4.css",
  "index-DCFNfpD5.js",
  "index-DSErXZpT.js",
  "index-kXmA3Tsf.js",
  "index-D-i-Dcu8.js"
]);

export async function onRequest(context) {
  const name = String(context.params.name || "");
  if (obsolete_assets.has(name)) {
    return new Response("Obsolete Codex Home Manager asset", {
      status: 410,
      headers: {
        "Cache-Control": "no-store, max-age=0",
        "Content-Type": "text/plain; charset=utf-8",
        "X-Robots-Tag": "noindex"
      }
    });
  }

  const asset_url = new URL(context.request.url);
  asset_url.search = "";
  return context.env.ASSETS.fetch(new Request(asset_url.toString(), context.request));
}

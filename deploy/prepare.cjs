// Helper for deploy/start.sh, run inside the n8n Docker image at every boot.
//
//   node prepare.cjs hash          prints a bcrypt hash of N8N_OWNER_PASSWORD
//   node prepare.cjs credentials   prints the n8n credentials to import
//                                  (Anthropic key + demo passcode)
//
// Values come from environment variables set in the Render dashboard.
// N8N_PKG is the n8n install folder, so we can borrow its bcryptjs library.
"use strict";

const need = (name) => {
  const v = (process.env[name] || "").trim();
  if (!v) { console.error(`Missing environment variable ${name}`); process.exit(1); }
  return v;
};

const mode = process.argv[2];
if (mode === "hash") {
  const bcrypt = require(require.resolve("bcryptjs", { paths: [need("N8N_PKG")] }));
  process.stdout.write(bcrypt.hashSync(need("N8N_OWNER_PASSWORD"), 10));
} else if (mode === "credentials") {
  // Same ids and names as n8n/setup_agent.ps1 and n8n/build_workflow.py.
  process.stdout.write(JSON.stringify([
    { id: "AnthropicCred001", name: "Anthropic account", type: "anthropicApi",
      data: { apiKey: need("ANTHROPIC_API_KEY") } },
    { id: "DemoPasscode001", name: "Demo passcode", type: "httpHeaderAuth",
      data: { name: "X-Passcode", value: need("DEMO_PASSCODE") } },
  ]));
} else {
  console.error("Usage: node prepare.cjs hash|credentials");
  process.exit(1);
}

# The hosted copy of the app: n8n (same version as package.json) plus our two
# workflows. Built by Render from render.yaml; see README "Put it online".
FROM n8nio/n8n:2.47.3

COPY deploy/start.sh deploy/prepare.cjs /home/node/deploy/
COPY n8n/complaint_agent.workflow.json n8n/demo_ui.workflow.json /home/node/deploy/

ENTRYPOINT ["/bin/sh", "/home/node/deploy/start.sh"]

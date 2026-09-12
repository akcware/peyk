# Peyk documentation

Start with the [project README](../README.md) for what Peyk is and why. These pages are for people who want
to run it, change it, or understand how it is built.

| Page | Read it when you want to… |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | understand how an event becomes (or does not become) a notification, what the agent package does, and why the code is shaped the way it is |
| [SETUP.md](SETUP.md) | run Peyk on your own machine: secrets, Composio, Bedrock, first message |
| [DEPLOY.md](DEPLOY.md) | run it in production (Fly.io + Neon + Bedrock) or on Bedrock AgentCore |
| [ENGINEERING.md](ENGINEERING.md) | know what the tests cover, how the phase gates work, and what was measured |
| [DEMO-SCRIPT.md](DEMO-SCRIPT.md) | record the five-minute demo video |
| [DEVPOST.md](DEVPOST.md) | read the hackathon submission text |

The `agent/` package has its own short [README](../agent/README.md): it is the only code that runs on Bedrock
AgentCore, and its contract with the workers is documented there. The AgentCore CLI's generic reference lives in
[agentcore/CLI-REFERENCE.md](../agentcore/CLI-REFERENCE.md).

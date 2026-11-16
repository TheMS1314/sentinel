# Sentinel dashboard

React/Vite dashboard for scoped policies, transaction decisions, human review, scenarios and recorded evaluations.

```bash
npm ci
npm test
npm run lint
npm run build
npm run dev
```

The development proxy forwards `/api` to localhost:8000. The Compose gateway serves the built dashboard and API under one origin. Sign in with a server-configured human credential; it is exchanged for an HttpOnly session. Do not bake credentials into `VITE_` variables or store them in localStorage.

Displayed risk scores are heuristics. Model confidence is self-reported. Evaluation cards show saved run metadata and do not infer live model use from provider name alone.

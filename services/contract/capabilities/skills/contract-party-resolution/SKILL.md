---
name: contract-party-resolution
description: Resolve PARTY_A and PARTY_B and map the user's selected perspective.
tags: [contract, party, neutral]
---

# Contract party resolution

Read the current Contract IR and source blocks before deciding. Return the exact registered JSON schema.
Identify the two signing sides from labels such as buyer/seller, principal/agent, or client/provider. Preserve
the source names. Respect the task's `perspective`; never swap it. If the selected side cannot be resolved
reliably, fail instead of inventing a party.

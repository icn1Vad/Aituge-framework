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

When `confirmed_party_a_name` and `confirmed_party_b_name` are both present in the task input, they are the
user-confirmed names for this formal review. Return those two names exactly in their corresponding fields and
derive `our_party` and `counterparty` from the selected perspective. Do not rename, swap, or supersede a
confirmed party. If either confirmed name is not supported by the current contract, fail rather than guessing.

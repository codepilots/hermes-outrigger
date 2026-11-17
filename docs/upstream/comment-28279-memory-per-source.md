# Comment for NousResearch/hermes-agent#28279

---

Another case of the same leak, from a platform plugin, and a small hook that
would let adapters close it without waiting for scoped memory.

**Setup.**
- [hermes-outrigger](https://github.com/codepilots/hermes-outrigger) registers a
  messaging platform for thin clients.
- Paired, non-owner users chat through it.
- Their tools are narrowed with the adapter's `toolsets_for_source()` (no
  `memory`, `terminal` or files). That works: `tool_call("memory", …)` is
  refused.

**Problem.** The owner's USER PROFILE (and MEMORY.md when built-in memory is on)
is still injected into the paired user's system prompt. Verified on 0.23.1: the
paired user's turn has a "USER PROFILE (who the user is)" section, so they can
just ask the agent what it knows about the owner.

**Cause.** `agent_init._init_memory` decides `_memory_enabled` and
`_user_profile_enabled` from the global `memory` config alone. An adapter can
narrow tools per source, but it has no say over the prompt blocks.

**Proposal.** A sibling to `toolsets_for_source`:

```python
def memory_for_source(self, source: SessionSource) -> Optional[dict]:
    """Per-source override of built-in memory injection, or None (default).
    e.g. {"memory": False, "user_profile": False} for a non-owner user."""
    return None
```

The gateway would apply it where it resolves toolsets for the turn, by
passing the flags into agent construction.

This complements #71224 rather than replacing it. Scoped entries decide *which*
memories belong where. This hook lets an adapter say "this source gets none of
the owner's", which is the safe default for shared bots and paired users. It
also needs no schema change.

If this is in line with what maintainers want, I'm happy to send a PR.

# Why Arkiv

The chat is an interface to work that survives a conversation. Arkiv holds the
checkpoints the application must query to list tasks and restart their execution.
Removing Arkiv breaks the default task list, task creation and recovery; payments
cannot run in the explicit SQLite-only development mode.

SQLite is an execution cache and keeps local conversations/preferences. It is not
a fallback task authority. An independent SDK client can discover the same
publisher's checkpoint records without our API. Authorized users need the separate
local encryption key to read private task contents.

This gives a concrete demonstration of externally queryable task state. It does
not claim that Arkiv signs payments, knows a MetaMask key, or eliminates key custody,
provider failures, transaction fees or testnet resets.

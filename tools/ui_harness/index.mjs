// Harness always mirrors the real app UI module. Keeping one source prevents browser evidence from
// accidentally proving a stale copy while the installed App runs different code.
export { default } from '/ui/index.mjs'

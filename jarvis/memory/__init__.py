"""F10 long-term memory: only what the developer tells her to remember,
stored locally, never sent online. Import from the submodules
(jarvis.memory.store, .commands, .sensitive): the logbook's redaction uses
.sensitive, so this package must not import the commands (which import the
interpreter, which imports the logbook)."""

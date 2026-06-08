# Record with: vhs docs/demo/sahara-demo.tape
# Install VHS: https://github.com/charmbracelet/vhs
#
# This recording runs Sahara against committed fictional documents. The hidden
# setup may download the embedding model on first use.

Output docs/images/sahara-cli-demo.gif

Set Shell "bash"
Set FontSize 17
Set Width 1200
Set Height 760
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms
Set PlaybackSpeed 1.0

Hide
Type "export SAHARA_REAL_HOME=\"$HOME\""
Enter
Type "export HOME=/tmp/sahara-readme-demo"
Enter
Type "export HF_HOME=\"$SAHARA_REAL_HOME/.cache/huggingface\""
Enter
Type "rm -rf \"$HOME/.sahara\" \"$HOME/Documents\" && mkdir -p \"$HOME/Documents\""
Enter
Type "cp -R docs/demo/fixtures/. \"$HOME/Documents/\""
Enter
Type "sahara init --mode basic --folder \"$HOME/Documents\""
Enter
Sleep 2s
Type "sahara index"
Enter
Sleep 15s
Show

Type "clear"
Enter
Sleep 1s

Type "sahara search \"who fixed the dishwasher\" --snippet"
Enter
Sleep 4s

Type "sahara search \"when was the Portugal trip\" --snippet"
Enter
Sleep 4s

Type "sahara ask --snippet \"what did the pediatrician diagnose last winter?\""
Enter
Sleep 5s

Type "sahara mcp install-claude"
Enter
Sleep 4s

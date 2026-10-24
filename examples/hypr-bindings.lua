-- Add to ~/.config/hypr/bindings.lua after reviewing existing bindings.
-- Back up that file first. Do not replace the file with this example.
-- If these keys already have global bindings, review them and call
-- hl.unbind("ALT + C") / hl.unbind("ALT + SHIFT + C") before rebinding.
o.bind("ALT + C", "Toggle Local Dictate", "$HOME/.local/bin/local-dictate toggle", { release = true })
o.bind("ALT + SHIFT + C", "Cancel Local Dictate", "$HOME/.local/bin/local-dictate cancel")

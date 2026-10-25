# Witcher 3 Remaster to Next-Gen Converter

This program converts save files from The Witcher 3 version 5.0 so that version 4.04 can load them.
The game cannot do this by itself, because version 5.0 stores its saves in a slightly different way.

The converter only changes what version 4.04 needs. It updates the version information, removes data that
only version 5.0 uses, and adjusts the inventory to the older layout. Because the skill tree in version 5.0
is different, it resets your skills and gives back all your skill points, so you can spend them again.
Equipped mutagens go back into your inventory. Everything else, such as your quest progress and your
items, stays as it was.

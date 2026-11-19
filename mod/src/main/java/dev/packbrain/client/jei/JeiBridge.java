package dev.packbrain.client.jei;

import net.minecraft.client.Minecraft;
import net.minecraft.network.chat.Component;
import net.neoforged.fml.ModList;

/** JEI-free entry point: only touches the JEI classes when JEI is actually installed. */
public final class JeiBridge {
    private JeiBridge() {
    }

    public static void show(String itemId) {
        boolean ok = ModList.get().isLoaded("jei") && JeiCompat.show(itemId);
        if (!ok && Minecraft.getInstance().player != null) {
            Minecraft.getInstance().player.displayClientMessage(Component.literal("JEI ist nicht verfuegbar (" + itemId + ")"), true);
        }
    }
}

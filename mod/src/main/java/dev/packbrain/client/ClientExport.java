package dev.packbrain.client;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.mojang.brigadier.context.CommandContext;
import com.mojang.logging.LogUtils;

import net.minecraft.client.Minecraft;
import net.minecraft.commands.CommandSourceStack;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.network.chat.Component;
import net.minecraft.world.item.Item;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.TooltipFlag;
import net.neoforged.fml.loading.FMLPaths;
import org.slf4j.Logger;

/**
 * /packbrain_client export  (client only, must be run inside a world):
 * writes packbrain-export/client_items_<language>.json with, for every item, the display name in the player's current game language
 * and the tooltip lines exactly as the game shows them (attribute lines like "+7 Attack Damage", mod-specific hints ...).
 * Items without a translation fall back to English. Run it once per language you want in the knowledge base.
 */
public final class ClientExport {
    private static final Logger LOGGER = LogUtils.getLogger();
    private static final Gson GSON = new GsonBuilder().disableHtmlEscaping().create();

    private ClientExport() {
    }

    public static int run(CommandContext<CommandSourceStack> ctx) {
        Minecraft mc = Minecraft.getInstance();
        if (mc.level == null || mc.player == null) {
            ctx.getSource().sendFailure(Component.literal("[Pack-Brain] Join a world first."));
            return 0;
        }
        Item.TooltipContext tooltipContext = Item.TooltipContext.of(mc.level);
        JsonObject root = new JsonObject();
        String lang = mc.getLanguageManager().getSelected();
        root.addProperty("lang", lang);
        JsonArray items = new JsonArray();
        int tooltipErrors = 0;
        for (Item item : BuiltInRegistries.ITEM) {
            ItemStack stack = item.getDefaultInstance();
            if (stack.isEmpty()) {
                continue;
            }
            JsonObject o = new JsonObject();
            o.addProperty("id", BuiltInRegistries.ITEM.getKey(item).toString());
            o.addProperty("name", stack.getHoverName().getString());
            try {
                List<Component> lines = stack.getTooltipLines(tooltipContext, mc.player, TooltipFlag.Default.NORMAL);
                JsonArray tl = new JsonArray();
                for (int i = 1; i < lines.size(); i++) {   // line 0 is the name
                    String s = lines.get(i).getString();
                    if (!s.isBlank()) {
                        tl.add(s);
                    }
                }
                if (tl.size() > 0) {
                    o.add("tooltip", tl);
                }
            } catch (RuntimeException e) {
                tooltipErrors++;                           // some mods build tooltips that need a full game context
            }
            items.add(o);
        }
        root.add("items", items);
        try {
            Path dir = FMLPaths.GAMEDIR.get().resolve("packbrain-export");
            Files.createDirectories(dir);
            Path file = dir.resolve("client_items_" + lang + ".json");
            Files.writeString(file, GSON.toJson(root), StandardCharsets.UTF_8);
            String msg = "[Pack-Brain] Exported " + items.size() + " items (language " + lang + ", " + tooltipErrors + " tooltips skipped) to " + file;
            ctx.getSource().sendSuccess(() -> Component.literal(msg), false);
            LOGGER.info(msg);
            return 1;
        } catch (IOException e) {
            LOGGER.error("Client export failed", e);
            ctx.getSource().sendFailure(Component.literal("[Pack-Brain] Export failed: " + e));
            return 0;
        }
    }
}

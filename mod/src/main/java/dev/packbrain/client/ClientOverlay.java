package dev.packbrain.client;

import java.util.List;

import dev.packbrain.net.AnswerChunk;
import net.minecraft.client.DeltaTracker;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.Font;
import net.minecraft.client.gui.GuiGraphics;
import net.minecraft.network.chat.Component;
import net.minecraft.network.chat.FormattedText;
import net.minecraft.util.FormattedCharSequence;

/** Answer panel in the top-left corner. Client only. State is touched on the client thread only. */
public final class ClientOverlay {
    private static final long WAIT_LIMIT_MS = 90_000;   // hide a request that never finishes
    private static final int MAX_LINES = 14;

    private static int activeRequest = -1;
    private static String title = "";
    private static String itemId = "";
    private static String status = "";
    private static final StringBuilder text = new StringBuilder();
    private static boolean done;
    private static long lastUpdate;
    private static long doneAt;
    private static long readMs;

    private ClientOverlay() {
    }

    /** Called by the hotkey before the request is sent. */
    public static void begin(int requestId, String itemName, String id) {
        activeRequest = requestId;
        title = itemName;
        itemId = id;
        status = Component.translatable("packbrain.thinking").getString();
        text.setLength(0);
        done = false;
        lastUpdate = System.currentTimeMillis();
    }

    public static void showMessage(String message) {
        activeRequest = -2;
        title = "Pack-Brain";
        status = "";
        text.setLength(0);
        text.append(message);
        itemId = "";
        finish(4_000);
    }

    /** Answer complete: stay readable for a time that depends on its length (8-25 s), then disappear. */
    private static void finish(long minMs) {
        done = true;
        doneAt = System.currentTimeMillis();
        lastUpdate = doneAt;
        readMs = Math.max(minMs, Math.min(25_000, 6_000 + 45L * text.length()));
    }

    /** True while the panel shows (or is loading) the explanation of this item: pressing the key again closes it. */
    public static boolean isShowing(String id) {
        return visible() && id != null && id.equals(itemId);
    }

    public static void hide() {
        activeRequest = -1;
    }

    public static boolean visible() {
        if (activeRequest == -1) {
            return false;
        }
        long now = System.currentTimeMillis();
        return done ? now - doneAt < readMs : now - lastUpdate < WAIT_LIMIT_MS;
    }

    public static void accept(AnswerChunk c) {
        if (ChatState.owns(c.requestId())) {   // answer for the question window (hotkey K)
            ChatState.accept(c);
            return;
        }
        if (c.kind() == AnswerChunk.ITEMS) {
            return;
        }
        if (c.requestId() != activeRequest) {
            return; // answer to an older request
        }
        lastUpdate = System.currentTimeMillis();
        switch (c.kind()) {
            case AnswerChunk.STATUS -> status = c.text();
            case AnswerChunk.TOKEN -> {
                status = "";
                text.append(c.text());
            }
            case AnswerChunk.RESET -> text.setLength(0);
            case AnswerChunk.DONE -> {
                status = "";
                text.setLength(0);
                text.append(c.text());
                finish(8_000);
            }
            case AnswerChunk.ERROR -> {
                status = "";
                text.setLength(0);
                text.append("§c").append(c.text());
                finish(6_000);
            }
            default -> {
            }
        }
    }

    /** Draws the panel. Used by the HUD layer (no screen open) and after screens render (inventory etc.). */
    public static void draw(GuiGraphics g) {
        if (!visible()) {
            return;
        }
        Minecraft mc = Minecraft.getInstance();
        Font font = mc.font;
        int width = Math.min(260, mc.getWindow().getGuiScaledWidth() / 2);
        String body = text.length() > 0 ? text.toString() : status;
        List<FormattedCharSequence> lines = font.split(FormattedText.of(body), width - 10);
        int shown = Math.min(lines.size(), MAX_LINES);
        int height = 14 + shown * (font.lineHeight + 1) + 6 + (lines.size() > MAX_LINES ? font.lineHeight : 0);
        int x = 6, y = 6;
        g.fill(x, y, x + width, y + height, 0xC0101018);
        g.fill(x, y, x + width, y + 1, 0xFF3FB6D9);
        g.drawString(font, "§bPack-Brain §7- " + title, x + 5, y + 4, 0xFFFFFF, false);
        int ty = y + 16;
        for (int i = 0; i < shown; i++) {
            g.drawString(font, lines.get(i), x + 5, ty, 0xFFFFFF, false);
            ty += font.lineHeight + 1;
        }
        if (lines.size() > MAX_LINES) {
            g.drawString(font, "§7...", x + 5, ty, 0xFFFFFF, false);
        }
    }

    /** HUD layer entry point (see ClientEvents). */
    public static void renderHud(GuiGraphics g, DeltaTracker delta) {
        Minecraft mc = Minecraft.getInstance();
        if (mc.screen == null && !mc.options.hideGui) {
            draw(g);
        }
    }
}

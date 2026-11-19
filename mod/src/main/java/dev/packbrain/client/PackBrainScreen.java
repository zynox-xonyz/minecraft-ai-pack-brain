package dev.packbrain.client;

import java.util.ArrayList;
import java.util.List;

import dev.packbrain.client.jei.JeiBridge;
import dev.packbrain.net.AskRequest;
import net.minecraft.client.gui.GuiGraphics;
import net.minecraft.client.gui.components.Button;
import net.minecraft.client.gui.components.EditBox;
import net.minecraft.client.gui.screens.Screen;
import net.minecraft.network.chat.Component;
import net.minecraft.network.chat.FormattedText;
import net.minecraft.util.FormattedCharSequence;
import net.neoforged.neoforge.network.PacketDistributor;
import org.lwjgl.glfw.GLFW;

/** The Pack-Brain window (hotkey K): type a question, the answer streams in; item names below an answer open JEI. */
public class PackBrainScreen extends Screen {
    private static final int MAX_WIDTH = 440;

    private EditBox input;
    private int scroll;                                     // rows scrolled up from the bottom
    private final List<int[]> chipBoxes = new ArrayList<>(); // x, y, w, h (parallel to chipIds)
    private final List<String> chipIds = new ArrayList<>();

    /** One rendered row: text, or a row of item chips. */
    private record Row(FormattedCharSequence text, int color, List<String[]> chips) {
    }

    public PackBrainScreen() {
        super(Component.literal("Pack-Brain"));
    }

    private int panelWidth() {
        return Math.min(MAX_WIDTH, width - 20);
    }

    private int left() {
        return (width - panelWidth()) / 2;
    }

    @Override
    protected void init() {
        int w = panelWidth();
        input = new EditBox(font, left(), height - 28, w - 72, 20, Component.literal("Frage"));
        input.setMaxLength(300);
        input.setHint(Component.literal("Frag mich etwas zu diesem Modpack ..."));
        addRenderableWidget(input);
        addRenderableWidget(Button.builder(Component.literal("Senden"), b -> send()).bounds(left() + w - 68, height - 28, 68, 20).build());
        setInitialFocus(input);
    }

    @Override
    public boolean keyPressed(int key, int scan, int mods) {
        if (key == GLFW.GLFW_KEY_ENTER || key == GLFW.GLFW_KEY_KP_ENTER) {
            send();
            return true;
        }
        return super.keyPressed(key, scan, mods);
    }

    private void send() {
        String q = input.getValue().strip();
        if (q.isEmpty() || minecraft == null) {
            return;
        }
        input.setValue("");
        scroll = 0;
        int id = ClientEvents.nextRequestId();
        ChatState.Entry e = ChatState.start(id, q);
        if (minecraft.getConnection() == null || !minecraft.getConnection().hasChannel(AskRequest.TYPE)) {
            e.answer.append(Component.translatable("packbrain.no_server_mod").getString());
            e.error = true;
            e.done = true;
            e.status = "";
            return;
        }
        PacketDistributor.sendToServer(new AskRequest(id, q));
    }

    private List<Row> buildRows(int textWidth) {
        List<Row> rows = new ArrayList<>();
        for (ChatState.Entry e : ChatState.entries()) {
            for (FormattedCharSequence line : font.split(FormattedText.of("Du: " + e.question), textWidth)) {
                rows.add(new Row(line, 0xFFE070, null));
            }
            String body = e.answer.length() > 0 ? e.answer.toString() : e.status;
            int color = e.error ? 0xFF6060 : (e.answer.length() > 0 ? 0xFFFFFF : 0x909090);
            for (FormattedCharSequence line : font.split(FormattedText.of(body), textWidth)) {
                rows.add(new Row(line, color, null));
            }
            if (e.done && !e.items.isEmpty()) {   // wrap chips into rows that fit the width
                List<String[]> current = new ArrayList<>();
                int x = font.width("Rezept: ");
                for (String[] it : e.items) {
                    int w = font.width("[" + it[1] + "] ");
                    if (x + w > textWidth && !current.isEmpty()) {
                        rows.add(new Row(null, 0, current));
                        current = new ArrayList<>();
                        x = font.width("Rezept: ");
                    }
                    current.add(it);
                    x += w;
                }
                rows.add(new Row(null, 0, current));
            }
            rows.add(new Row(FormattedCharSequence.EMPTY, 0, null));
        }
        return rows;
    }

    @Override
    public void render(GuiGraphics g, int mouseX, int mouseY, float partialTick) {
        super.render(g, mouseX, mouseY, partialTick);
        int w = panelWidth();
        int left = left();
        int top = 20;
        int bottom = height - 36;
        g.fill(left - 4, top - 4, left + w + 4, bottom + 4, 0xC0101018);
        g.drawString(font, "Pack-Brain  -  Fragen zum Modpack (Enter = senden, Mausrad = scrollen)", left, 7, 0x3FB6D9, false);
        int line = font.lineHeight + 1;
        List<Row> rows = buildRows(w - 8);
        int visible = Math.max(1, (bottom - top) / line);
        scroll = Math.max(0, Math.min(scroll, Math.max(0, rows.size() - visible)));
        int end = rows.size() - scroll;
        int start = Math.max(0, end - visible);
        chipBoxes.clear();
        chipIds.clear();
        int y = top;
        if (rows.isEmpty()) {
            g.drawString(font, "Zum Beispiel: \"wie mach ich messing\", \"was soll ich als naechstes machen\", \"staerkstes schwert\"", left, y, 0x909090, false);
        }
        for (int i = start; i < end; i++) {
            Row r = rows.get(i);
            if (r.chips() != null) {
                int x = left;
                g.drawString(font, "Rezept: ", x, y, 0x909090, false);
                x += font.width("Rezept: ");
                for (String[] it : r.chips()) {
                    String label = "[" + it[1] + "]";
                    int cw = font.width(label);
                    boolean hover = mouseX >= x && mouseX < x + cw && mouseY >= y && mouseY < y + font.lineHeight;
                    g.drawString(font, label, x, y, hover ? 0xFFFFFF : 0x55DDFF, false);
                    chipBoxes.add(new int[]{x, y, cw, font.lineHeight});
                    chipIds.add(it[0]);
                    x += cw + font.width(" ");
                }
            } else if (r.text() != null) {
                g.drawString(font, r.text(), left, y, r.color(), false);
            }
            y += line;
        }
    }

    @Override
    public boolean mouseClicked(double mouseX, double mouseY, int button) {
        for (int i = 0; i < chipBoxes.size(); i++) {
            int[] b = chipBoxes.get(i);
            if (mouseX >= b[0] && mouseX < b[0] + b[2] && mouseY >= b[1] && mouseY < b[1] + b[3]) {
                JeiBridge.show(chipIds.get(i));
                return true;
            }
        }
        return super.mouseClicked(mouseX, mouseY, button);
    }

    @Override
    public boolean mouseScrolled(double mouseX, double mouseY, double scrollX, double scrollY) {
        scroll += scrollY > 0 ? 3 : -3;
        return true;
    }

    @Override
    public boolean isPauseScreen() {
        return false;
    }
}

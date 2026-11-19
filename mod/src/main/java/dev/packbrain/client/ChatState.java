package dev.packbrain.client;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import com.google.gson.JsonElement;
import com.google.gson.JsonParser;

import dev.packbrain.net.AnswerChunk;

/** Conversation shown in the Pack-Brain window (hotkey K). Client thread only. */
public final class ChatState {
    public static final class Entry {
        public final int id;
        public final String question;
        public final StringBuilder answer = new StringBuilder();
        public String status = "";
        public boolean done;
        public boolean error;
        public final List<String[]> items = new ArrayList<>();   // {id, name}

        Entry(int id, String question) {
            this.id = id;
            this.question = question;
        }
    }

    private static final int MAX = 20;
    private static final List<Entry> ENTRIES = new ArrayList<>();
    private static final Map<Integer, Entry> BY_ID = new HashMap<>();

    private ChatState() {
    }

    public static Entry start(int id, String question) {
        Entry e = new Entry(id, question);
        e.status = "...";
        ENTRIES.add(e);
        BY_ID.put(id, e);
        while (ENTRIES.size() > MAX) {
            BY_ID.remove(ENTRIES.remove(0).id);
        }
        return e;
    }

    public static boolean owns(int id) {
        return BY_ID.containsKey(id);
    }

    public static List<Entry> entries() {
        return ENTRIES;
    }

    public static void clear() {
        ENTRIES.clear();
        BY_ID.clear();
    }

    public static void accept(AnswerChunk c) {
        Entry e = BY_ID.get(c.requestId());
        if (e == null) {
            return;
        }
        switch (c.kind()) {
            case AnswerChunk.STATUS -> e.status = c.text();
            case AnswerChunk.TOKEN -> {
                e.status = "";
                e.answer.append(c.text());
            }
            case AnswerChunk.RESET -> e.answer.setLength(0);
            case AnswerChunk.DONE -> {
                e.status = "";
                e.answer.setLength(0);
                e.answer.append(c.text());
                e.done = true;
            }
            case AnswerChunk.ERROR -> {
                e.status = "";
                e.answer.setLength(0);
                e.answer.append(c.text());
                e.error = true;
                e.done = true;
            }
            case AnswerChunk.ITEMS -> {
                try {
                    for (JsonElement el : JsonParser.parseString(c.text()).getAsJsonArray()) {
                        e.items.add(new String[]{el.getAsJsonObject().get("id").getAsString(), el.getAsJsonObject().get("name").getAsString()});
                    }
                } catch (RuntimeException ignored) {
                    // malformed list: just no links
                }
            }
            default -> {
            }
        }
    }
}

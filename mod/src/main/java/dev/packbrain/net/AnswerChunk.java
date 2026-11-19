package dev.packbrain.net;

import dev.packbrain.PackBrainMod;
import io.netty.buffer.ByteBuf;
import net.minecraft.network.codec.ByteBufCodecs;
import net.minecraft.network.codec.StreamCodec;
import net.minecraft.network.protocol.common.custom.CustomPacketPayload;
import net.minecraft.resources.ResourceLocation;

/** Server -> client: one piece of a streamed answer for the overlay. */
public record AnswerChunk(int requestId, int kind, String text) implements CustomPacketPayload {
    public static final int STATUS = 0;  // text = short status line ("searching ...")
    public static final int TOKEN = 1;   // text = next piece of the answer
    public static final int RESET = 2;   // discard the answer text so far
    public static final int DONE = 3;    // text = complete clean answer
    public static final int ERROR = 4;   // text = message for the player
    public static final int ITEMS = 5;   // text = JSON array [{id, name}] of items mentioned in the answer (JEI links)

    public static final Type<AnswerChunk> TYPE = new Type<>(ResourceLocation.fromNamespaceAndPath(PackBrainMod.MODID, "answer_chunk"));
    public static final StreamCodec<ByteBuf, AnswerChunk> CODEC = StreamCodec.composite(
            ByteBufCodecs.VAR_INT, AnswerChunk::requestId,
            ByteBufCodecs.VAR_INT, AnswerChunk::kind,
            ByteBufCodecs.STRING_UTF8, AnswerChunk::text,
            AnswerChunk::new);

    @Override
    public Type<? extends CustomPacketPayload> type() {
        return TYPE;
    }
}

package dev.packbrain.net;

import dev.packbrain.PackBrainMod;
import io.netty.buffer.ByteBuf;
import net.minecraft.network.codec.ByteBufCodecs;
import net.minecraft.network.codec.StreamCodec;
import net.minecraft.network.protocol.common.custom.CustomPacketPayload;
import net.minecraft.resources.ResourceLocation;

/** Client -> server: "what is this place?" (location hotkey). The server reads position, structures and mobs itself. */
public record LocateRequest(int requestId) implements CustomPacketPayload {
    public static final Type<LocateRequest> TYPE = new Type<>(ResourceLocation.fromNamespaceAndPath(PackBrainMod.MODID, "locate_request"));
    public static final StreamCodec<ByteBuf, LocateRequest> CODEC = StreamCodec.composite(
            ByteBufCodecs.VAR_INT, LocateRequest::requestId,
            LocateRequest::new);

    @Override
    public Type<? extends CustomPacketPayload> type() {
        return TYPE;
    }
}

package dev.packbrain.client;

import com.mojang.blaze3d.platform.InputConstants;
import com.mojang.brigadier.arguments.StringArgumentType;

import dev.packbrain.PackBrainMod;
import dev.packbrain.client.jei.JeiBridge;
import dev.packbrain.net.ExplainRequest;
import dev.packbrain.net.LocateRequest;
import net.minecraft.client.KeyMapping;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.components.EditBox;
import net.minecraft.client.gui.screens.Screen;
import net.minecraft.client.gui.screens.inventory.AbstractContainerScreen;
import net.minecraft.commands.Commands;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.network.chat.Component;
import net.minecraft.resources.ResourceLocation;
import net.minecraft.world.inventory.Slot;
import net.minecraft.world.item.Item;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.HitResult;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.neoforge.client.event.ClientTickEvent;
import net.neoforged.neoforge.client.event.RegisterClientCommandsEvent;
import net.neoforged.neoforge.client.event.RegisterGuiLayersEvent;
import net.neoforged.neoforge.client.event.RegisterKeyMappingsEvent;
import net.neoforged.neoforge.client.event.ScreenEvent;
import net.neoforged.neoforge.client.settings.KeyConflictContext;
import net.neoforged.neoforge.network.PacketDistributor;
import org.lwjgl.glfw.GLFW;

/** Client only: hotkeys (H = explain item, K = question window), overlay, client commands. */
public final class ClientEvents {
    public static final KeyMapping EXPLAIN = new KeyMapping("key.packbrain.explain", KeyConflictContext.UNIVERSAL,
            InputConstants.Type.KEYSYM, GLFW.GLFW_KEY_H, "key.categories.packbrain");
    public static final KeyMapping CHAT = new KeyMapping("key.packbrain.chat", KeyConflictContext.IN_GAME,
            InputConstants.Type.KEYSYM, GLFW.GLFW_KEY_K, "key.categories.packbrain");
    public static final KeyMapping LOCATE = new KeyMapping("key.packbrain.locate", KeyConflictContext.IN_GAME,
            InputConstants.Type.KEYSYM, GLFW.GLFW_KEY_O, "key.categories.packbrain");
    private static int nextRequest = 1;
    private static String pendingJei;   // set by a clicked chat link; opened on the next tick

    private ClientEvents() {
    }

    public static int nextRequestId() {
        return nextRequest++;
    }

    /** Mod bus: key mappings and HUD layer registration. */
    @EventBusSubscriber(modid = PackBrainMod.MODID, bus = EventBusSubscriber.Bus.MOD, value = Dist.CLIENT)
    public static class ModBus {
        @SubscribeEvent
        public static void keys(RegisterKeyMappingsEvent e) {
            e.register(EXPLAIN);
            e.register(CHAT);
            e.register(LOCATE);
        }

        @SubscribeEvent
        public static void layers(RegisterGuiLayersEvent e) {
            e.registerAboveAll(ResourceLocation.fromNamespaceAndPath(PackBrainMod.MODID, "answer"), ClientOverlay::renderHud);
        }
    }

    /** Game bus: hotkeys, chat links, drawing the panel above screens. */
    @EventBusSubscriber(modid = PackBrainMod.MODID, value = Dist.CLIENT)
    public static class GameBus {
        @SubscribeEvent
        public static void tick(ClientTickEvent.Post e) {
            Minecraft mc = Minecraft.getInstance();
            if (pendingJei != null) {
                String id = pendingJei;
                pendingJei = null;
                JeiBridge.show(id);
            }
            while (LOCATE.consumeClick()) {   // "what is this place": structure, biome, enemies, loot quality
                if (mc.player == null || mc.screen != null) {
                    continue;
                }
                if (ClientOverlay.isShowing("@location")) {
                    ClientOverlay.hide();
                } else if (mc.getConnection() == null || !mc.getConnection().hasChannel(LocateRequest.TYPE)) {
                    ClientOverlay.showMessage(Component.translatable("packbrain.no_server_mod").getString());
                } else {
                    int req = nextRequestId();
                    ClientOverlay.begin(req, Component.translatable("packbrain.location").getString(), "@location");
                    PacketDistributor.sendToServer(new LocateRequest(req));
                }
            }
            while (CHAT.consumeClick()) {
                if (mc.player != null && mc.screen == null) {
                    mc.setScreen(new PackBrainScreen());
                }
            }
            while (EXPLAIN.consumeClick()) {   // no screen open: held item, otherwise the block under the crosshair
                if (mc.player == null || mc.screen != null) {
                    continue;
                }
                ItemStack held = mc.player.getMainHandItem();
                if (!held.isEmpty()) {
                    explain(held.getItem(), held.getHoverName().getString());
                } else if (mc.hitResult instanceof BlockHitResult hit && hit.getType() == HitResult.Type.BLOCK && mc.level != null) {
                    Item item = mc.level.getBlockState(hit.getBlockPos()).getBlock().asItem();
                    ItemStack st = item.getDefaultInstance();
                    if (!st.isEmpty()) {
                        explain(item, st.getHoverName().getString());
                    }
                } else if (ClientOverlay.visible()) {
                    ClientOverlay.hide();
                } else {
                    ClientOverlay.showMessage("Halte ein Item in der Hand oder schau einen Block an.");
                }
            }
        }

        /** Inventory-like screens: item under the mouse. */
        @SubscribeEvent
        public static void screenKey(ScreenEvent.KeyPressed.Pre e) {
            Screen screen = e.getScreen();
            if (!(screen instanceof AbstractContainerScreen<?> container) || screen.getFocused() instanceof EditBox) {
                return;
            }
            if (!EXPLAIN.matches(e.getKeyCode(), e.getScanCode())) {
                return;
            }
            Slot slot = container.getSlotUnderMouse();
            if (slot != null && slot.hasItem()) {
                ItemStack st = slot.getItem();
                explain(st.getItem(), st.getHoverName().getString());
                e.setCanceled(true);
            }
        }

        @SubscribeEvent
        public static void clientCommands(RegisterClientCommandsEvent e) {
            e.getDispatcher().register(Commands.literal("packbrain_client")
                    .then(Commands.literal("export").executes(ClientExport::run))
                    .then(Commands.literal("jei").then(Commands.argument("item", StringArgumentType.greedyString()).executes(ctx -> {
                        pendingJei = StringArgumentType.getString(ctx, "item").strip();
                        return 1;
                    }))));
        }

        @SubscribeEvent
        public static void drawOverScreens(ScreenEvent.Render.Post e) {
            if (!(e.getScreen() instanceof PackBrainScreen)) {
                ClientOverlay.draw(e.getGuiGraphics());
            }
        }
    }

    private static void explain(Item item, String displayName) {
        Minecraft mc = Minecraft.getInstance();
        if (mc.getConnection() == null || !mc.getConnection().hasChannel(ExplainRequest.TYPE)) {
            ClientOverlay.showMessage(Component.translatable("packbrain.no_server_mod").getString());
            return;
        }
        String id = BuiltInRegistries.ITEM.getKey(item).toString();
        if (ClientOverlay.isShowing(id)) {   // second press on the same item closes the panel
            ClientOverlay.hide();
            return;
        }
        int req = nextRequestId();
        ClientOverlay.begin(req, displayName, id);
        PacketDistributor.sendToServer(new ExplainRequest(req, id));
    }
}

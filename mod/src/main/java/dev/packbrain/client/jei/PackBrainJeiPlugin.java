package dev.packbrain.client.jei;

import dev.packbrain.PackBrainMod;
import mezz.jei.api.IModPlugin;
import mezz.jei.api.JeiPlugin;
import mezz.jei.api.runtime.IJeiRuntime;
import net.minecraft.resources.ResourceLocation;

/** Discovered by JEI through the annotation; never loaded when JEI is absent. */
@JeiPlugin
public class PackBrainJeiPlugin implements IModPlugin {
    @Override
    public ResourceLocation getPluginUid() {
        return ResourceLocation.fromNamespaceAndPath(PackBrainMod.MODID, "jei");
    }

    @Override
    public void onRuntimeAvailable(IJeiRuntime jeiRuntime) {
        JeiCompat.runtime = jeiRuntime;
    }

    @Override
    public void onRuntimeUnavailable() {
        JeiCompat.runtime = null;
    }
}

package dev.packbrain.client.jei;

import mezz.jei.api.constants.VanillaTypes;
import mezz.jei.api.recipe.IFocus;
import mezz.jei.api.recipe.RecipeIngredientRole;
import mezz.jei.api.runtime.IJeiRuntime;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.resources.ResourceLocation;
import net.minecraft.world.item.Item;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.Items;

/** Uses the JEI runtime (set by PackBrainJeiPlugin). Loaded only when JEI is present. */
public final class JeiCompat {
    static IJeiRuntime runtime;

    private JeiCompat() {
    }

    /** Opens JEI's recipe view for the item (how it is made). */
    static boolean show(String itemId) {
        if (runtime == null) {
            return false;
        }
        ResourceLocation rl = ResourceLocation.tryParse(itemId);
        if (rl == null) {
            return false;
        }
        Item item = BuiltInRegistries.ITEM.get(rl);
        if (item == Items.AIR) {
            return false;
        }
        IFocus<ItemStack> focus = runtime.getJeiHelpers().getFocusFactory()
                .createFocus(RecipeIngredientRole.OUTPUT, VanillaTypes.ITEM_STACK, new ItemStack(item));
        runtime.getRecipesGui().show(focus);
        return true;
    }
}

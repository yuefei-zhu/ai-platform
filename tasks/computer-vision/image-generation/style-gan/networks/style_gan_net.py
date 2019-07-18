import numpy as np
import torch
import torch.nn as nn
from collections import OrderedDict
from networks.custom_layers import EqualizedLinear, EqualizedConv2d, \
    NormalizationLayer
from networks.building_blocks import EarlySynthesisBlock, LaterSynthesisBlock, \
    EarlyDiscriminatorBlock, LaterDiscriminatorBlock

class MappingNet(nn.Sequential):
    """
    A mapping network f implemented using an 8-layer MLP
    """
    def __init__(self,
                 resolution            = 1024,
                 num_layers            = 8,
                 dlatent_size          = 512,
                 normalize_latents     = True,
                 nonlinearity          = 'lrelu',
                 maping_lrmul          = 0.01,      # We thus reduce the learning rate by two orders of magnitude for the mapping network
                 **kwargs):                         # other parameters are ignored

        resolution_log2: int = int(np.log2(resolution))

        assert resolution == 2**resolution_log2 and 4 <= resolution <= 1024

        act = {
            'relu': torch.relu,
            'lrelu': nn.LeakyReLU(negative_slope=0.2)
        }[nonlinearity]

        self.dlatent_broadcast = resolution_log2 * 2 - 2
        layers = []
        if normalize_latents:
            layers.append(('pixel_norm', NormalizationLayer()))
        for i in range(num_layers):
            layers.append(('dense{}'.format(i), EqualizedLinear(dlatent_size,
                                                                dlatent_size,
                                                                use_wscale=True,
                                                                lrmul=maping_lrmul)))
            layers.append(('dense{}_act'.format(i), act))

        super().__init__(OrderedDict(layers))

    def forward(self, x):
        # N x 512
        w = super().forward(x)
        if self.dlatent_broadcast is not None:
            # broadcast
            # tf.tile in the official tf implementation:
            # w = tf.tile(x[:, np.newaxis], [1, dlatent_broadcast, 1])
            w = w.unsqueeze(1).expand(-1, self.dlatent_broadcast, -1)
        return w


class SynthesisNet(nn.Module):
    """
    Synthesis network
    """
    def __init__(self,
                 dlatent_size       = 512,
                 num_channels       = 3,
                 resolution         = 1024,
                 fmap_base          = 8192,
                 fmap_decay         = 1.0,
                 fmap_max           = 512,
                 use_styles         = True,
                 const_input_layer  = True,
                 use_noise          = True,
                 nonlinearity       = 'lrelu',
                 use_wscale         = True,
                 use_pixel_norm     = False,
                 use_instance_norm  = True,
                 blur_filter        = [1, 2, 1],            # low-pass filer to apply when resampling activations. None = no filtering
                 **kwargs                                   # other parameters are ignored
                 ):
        super(SynthesisNet, self).__init__()

        # copied from tf implementation

        resolution_log2: int = int(np.log2(resolution))

        assert resolution == 2**resolution_log2 and 4 <= resolution <= 1024

        def nf(stage): return min(int(fmap_base / (2.0 ** (stage * fmap_decay))), fmap_max)

        act = {
            'relu': torch.relu,
            'lrelu': nn.LeakyReLU(negative_slope=0.2)
        }[nonlinearity]

        num_layers = resolution_log2 * 2 - 2

        num_styles = num_layers if use_styles else 1

        blocks = []
        torgbs = []

        # 2....10 (inclusive) for 1024 resolution
        for res in range(2, resolution_log2 + 1):
            channels = nf(res - 1)
            block_name = '{s}x{s}'.format(s=2**res)
            torgb_name = 'torgb_lod{}'.format(resolution_log2 - res)
            if res == 2:
                # early block
                block = (block_name, EarlySynthesisBlock(channels,
                                                         dlatent_size,
                                                         const_input_layer,
                                                         use_wscale,
                                                         use_noise,
                                                         use_pixel_norm,
                                                         use_instance_norm,
                                                         use_styles,
                                                         nonlinearity))
            else:
                block = (block_name, LaterSynthesisBlock(last_channels,
                                                         out_channels=channels,
                                                         dlatent_size=dlatent_size,
                                                         use_wscale=use_wscale,
                                                         use_noise=use_noise,
                                                         use_pixel_norm=use_pixel_norm,
                                                         use_instance_norm=use_instance_norm,
                                                         use_styles=use_styles,
                                                         nonlinearity=nonlinearity,
                                                         blur_filter=blur_filter,
                                                         res=res,
                                                         ))

            # torgb block
            torgb = (torgb_name, EqualizedConv2d(channels, num_channels, 1, use_wscale=use_wscale))

            blocks.append(block)
            torgbs.append(torgb)
            last_channels = channels

        # the last one has bias
        self.torgbs = nn.ModuleDict(OrderedDict(torgbs))

        #self.torgb = Upscale2dConv2d2(channels, num_channels, 1, gain=1, use_wscale=use_wscale, bias=True)
        self.blocks = nn.ModuleDict(OrderedDict(blocks))


    def forward(self, dlatents, step=0):
        for i, b in enumerate(self.blocks.values()):
            if i == 0:
                x = b(dlatents)
            else:
                x = b(x, dlatents)

        rgb = self.torgb(x)

        return rgb


# a convenient wrapping class
class Generator(nn.Sequential):
    def __init__(self, **kwargs):
        super().__init__(OrderedDict([
            ('g_mapping', MappingNet(**kwargs)),
            ('g_synthesis', SynthesisNet(**kwargs))
        ]))


class BasicDiscriminator(nn.Sequential):

    def __init__(self,
                 num_channels       = 3,
                 resolution         = 1024,
                 fmap_base          = 8192,
                 fmap_decay         = 1.0,
                 fmap_max           = 512,
                 nonlinearity       = 'lrelu',
                 mbstd_group_size   = 4,
                 mbstd_num_features = 1,
                 use_wscale         = True,
                 fused_scale        = 'auto',
                 blur_filter        =  [1, 2, 1],
                 ):
        resolution_log2: int = int(np.log2(resolution))

        assert resolution == 2**resolution_log2 and 4 <= resolution <= 1024

        def nf(stage): return min(int(fmap_base / (2.0 ** (stage * fmap_decay))), fmap_max)

        act = {
            'relu': torch.relu,
            'lrelu': nn.LeakyReLU(negative_slope=0.2)
        }[nonlinearity]
        # this is fixed. We need to grow it...
        layers = []
        layers.append(('fromrgb', EqualizedConv2d(num_channels, nf(resolution_log2-1), 1, use_wscale=use_wscale)))
        layers.append(('act', act))
        for res in range(resolution_log2, 2, -1):
            layers.append(('{s}x{s}'.format(s=2**res), EarlyDiscriminatorBlock(res=res,
                                                                               in_channels=nf(res-1),
                                                                               out_channels=nf(res-2),
                                                                               use_wscale=use_wscale,
                                                                               blur_filter=blur_filter,
                                                                               fused_scale=fused_scale,
                                                                               nonlinearity=nonlinearity)))
        layers.append(('4x4', LaterDiscriminatorBlock(in_channels=nf(2),
                                                      out_channels=1,
                                                      mbstd_group_size=mbstd_group_size,
                                                      mbstd_num_features=mbstd_num_features,
                                                      use_wscale=use_wscale,
                                                      nonlinearity=nonlinearity,
                                                      res=2
                                                      )))

        super().__init__(OrderedDict(layers))

import sys
import os
import argparse
from PIL import Image, ImageDraw
from Reader import Reader, decompress, ReadByte, ReadUint16, ReadInt16, ReadUint32, ReadInt32, ReadString


# Fixed version for both highres and lowres tex (also re-written for better support)
# Thanks to umop for his script and Knobse for KosmoSc and help

def WriteShape(spritedata, sheetdata, ShapeCount, TotalsTexture, filein):
    pathout = path_out(filein)
    maxLeft = 0
    maxRight = 0
    maxAbove = 0
    maxBelow = 0
    spriteglobals = {'SpriteWidth': 0, 'SpriteHeight': 0, 'GlobalZeroX': 0, 'GlobalZeroY': 0}
    for x in range(ShapeCount):
        for y in range(spritedata[x]['TotalRegions']):

            regionMinX = 32767
            regionMaxX = -32767
            regionMinY = 32767
            regionMaxY = -32767
            for z in range(spritedata[x]['Regions'][y]['NumPoints']):
                tmpX = spritedata[x]['Regions'][y]['ShapePoints'][z]['x']
                tmpY = spritedata[x]['Regions'][y]['ShapePoints'][z]['y']

                spritedata[x]['Regions'][y]['Top'] = tmpY if tmpY > spritedata[x]['Regions'][y]['Top'] else \
                spritedata[x]['Regions'][y]['Top']
                spritedata[x]['Regions'][y]['Left'] = tmpX if tmpX < spritedata[x]['Regions'][y]['Left'] else \
                spritedata[x]['Regions'][y]['Left']
                spritedata[x]['Regions'][y]['Bottom'] = tmpY if tmpY < spritedata[x]['Regions'][y]['Bottom'] else \
                spritedata[x]['Regions'][y]['Bottom']
                spritedata[x]['Regions'][y]['Right'] = tmpX if tmpX > spritedata[x]['Regions'][y]['Right'] else \
                spritedata[x]['Regions'][y]['Right']

                tmpX = spritedata[x]['Regions'][y]['SheetPoints'][z]['x']
                tmpY = spritedata[x]['Regions'][y]['SheetPoints'][z]['y']

                regionMinX = tmpX if tmpX < regionMinX else regionMinX
                regionMaxX = tmpX if tmpX > regionMaxX else regionMaxX
                regionMinY = tmpY if tmpY < regionMinY else regionMinY
                regionMaxY = tmpY if tmpY > regionMaxY else regionMaxY

            spritedata[x]['Regions'][y] = region_rotation(spritedata[x]['Regions'][y])

            if (spritedata[x]['Regions'][y]['Rotation'] == 90 or spritedata[x]['Regions'][y]['Rotation'] == 270):
                spritedata[x]['Regions'][y]['SpriteWidth'] = regionMaxY - regionMinY
                spritedata[x]['Regions'][y]['SpriteHeight'] = regionMaxX - regionMinX
            else:
                spritedata[x]['Regions'][y]['SpriteWidth'] = regionMaxX - regionMinX
                spritedata[x]['Regions'][y]['SpriteHeight'] = regionMaxY - regionMinY

            tmpX = spritedata[x]['Regions'][y]['SpriteWidth']
            tmpY = spritedata[x]['Regions'][y]['SpriteHeight']

            # determine origin pixel (0,0)
            try:
                spritedata[x]['Regions'][y]['RegionZeroX'] = \
                    int(round(abs(spritedata[x]['Regions'][y]['Left']) * (
                                tmpX / (spritedata[x]['Regions'][y]['Right'] - spritedata[x]['Regions'][y]['Left']))))
            except ZeroDivisionError:
                spritedata[x]['Regions'][y]['RegionZeroX'] = 0
                
            try:
                spritedata[x]['Regions'][y]['RegionZeroY'] = \
                    int(round(abs(spritedata[x]['Regions'][y]['Bottom']) * (
                                tmpY / (spritedata[x]['Regions'][y]['Top'] - spritedata[x]['Regions'][y]['Bottom']))))
            except ZeroDivisionError:
                spritedata[x]['Regions'][y]['RegionZeroY'] = 0

            # sprite image dimensions
            # max sprite size is determined from the zero points
            # the higher the 0, more pixels to the left/top are required
            # the higher the diff between the 0 and the image width/height, more pixels to the right/bottom are required
            maxLeft = spritedata[x]['Regions'][y]['RegionZeroX'] if spritedata[x]['Regions'][y][
                                                                        'RegionZeroX'] > maxLeft else maxLeft
            maxAbove = spritedata[x]['Regions'][y]['RegionZeroY'] if spritedata[x]['Regions'][y][
                                                                         'RegionZeroY'] > maxAbove else maxAbove
            tmpX = spritedata[x]['Regions'][y]['SpriteWidth'] - spritedata[x]['Regions'][y]['RegionZeroX']
            tmpY = spritedata[x]['Regions'][y]['SpriteHeight'] - spritedata[x]['Regions'][y]['RegionZeroY']
            maxRight = tmpX if tmpX > maxRight else maxRight
            maxBelow = tmpY if tmpY > maxBelow else maxBelow

    spriteglobals['SpriteWidth'] = maxLeft + maxRight
    spriteglobals['SpriteHeight'] = maxAbove + maxBelow
    spriteglobals['GlobalZeroX'] = maxLeft
    spriteglobals['GlobalZeroY'] = maxAbove

    # seems like final sprite size takes into account the mask's line, so we add 2 to each dimension
    spriteglobals['SpriteWidth'] += 2
    spriteglobals['SpriteHeight'] += 2

    maxrange = len(str(ShapeCount))

    #
    # third: all data gathered, time to start cutting
    #

    sheetimage = []
    base_filename = os.path.splitext(os.path.basename(filein))[0]
    for x in range(TotalsTexture):
        img_path = find_texture(filein, x)
        if img_path:
            sheetimage.append(Image.open(img_path).convert('RGBA'))
        else:
            print(f"Warning: Texture {x} not found for {filein}")
            # Add a dummy image to keep indices correct
            sheetimage.append(None)

    for x in range(ShapeCount):
        outImage = Image.new('RGBA', (spriteglobals['SpriteWidth'], spriteglobals['SpriteHeight']), None)
        has_content = False

        for y in range(spritedata[x]['TotalRegions']):
            sheetID = spritedata[x]['Regions'][y]['SheetID']
            if sheetID >= len(sheetimage) or sheetimage[sheetID] is None:
                continue

            polygon = []
            for z in range(spritedata[x]['Regions'][y]['NumPoints']):
                polygon.append((spritedata[x]['Regions'][y]['SheetPoints'][z]['x'],
                                spritedata[x]['Regions'][y]['SheetPoints'][z]['y']))

            if not polygon: continue

            imMask = Image.new('L', (sheetimage[sheetID].width, sheetimage[sheetID].height), 0)
            ImageDraw.Draw(imMask).polygon(polygon, fill=255)
            bbox = imMask.getbbox()
            if not bbox: continue
            
            regionsize = (bbox[2] - bbox[0], bbox[3] - bbox[1])
            imMask = imMask.crop(bbox)

            tmpRegion = Image.new('RGBA', regionsize, None)
            tmpRegion.paste(sheetimage[sheetID].crop(bbox), None, imMask)
            
            if (spritedata[x]['Regions'][y]['Mirroring'] == 1):
                tmpRegion = tmpRegion.transpose(Image.FLIP_LEFT_RIGHT)

            if spritedata[x]['Regions'][y]['Rotation'] != 0:
                tmpRegion = tmpRegion.rotate(spritedata[x]['Regions'][y]['Rotation'], expand=True)

            pasteLeft = spriteglobals['GlobalZeroX'] - spritedata[x]['Regions'][y]['RegionZeroX']
            pasteTop = spriteglobals['GlobalZeroY'] - spritedata[x]['Regions'][y]['RegionZeroY']

            outImage.paste(tmpRegion, (pasteLeft, pasteTop), tmpRegion)
            has_content = True
            
        if has_content:
            outImage.save(os.path.join(pathout, f"{base_filename}_sprite_{str(x).rjust(maxrange, '0')}.png"))

    print('done')


def find_texture(filein, index):
    base = os.path.splitext(filein)[0]
    suffix = (index * '_')
    candidates = [
        f"{base}_tex{suffix}.png",
        f"{base}_tex{index}.png",
        f"{base.replace('.sc', '')}_tex{suffix}.png",
        f"{base}_tex.png" if index == 0 else None
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def path_out(filein):
    base_name = os.path.splitext(os.path.basename(filein))[0]
    pathout = os.path.join(os.getcwd(), base_name + '_out')
    if not (os.path.exists(pathout)):
        os.makedirs(pathout)
    return pathout


def region_rotation(region):
    sumSheet = 0
    sumShape = 0
    for z in range(region['NumPoints']):
        p1_sheet = region['SheetPoints'][z]
        p2_sheet = region['SheetPoints'][(z + 1) % (region['NumPoints'])]
        sumSheet += (p2_sheet['x'] - p1_sheet['x']) * (p2_sheet['y'] + p1_sheet['y'])
        
        p1_shape = region['ShapePoints'][z]
        p2_shape = region['ShapePoints'][(z + 1) % (region['NumPoints'])]
        sumShape += (p2_shape['x'] - p1_shape['x']) * (p2_shape['y'] + p1_shape['y'])

    sheetOrientation = -1 if (sumSheet < 0) else 1
    shapeOrientation = -1 if (sumShape < 0) else 1

    region['Mirroring'] = 0 if (shapeOrientation == sheetOrientation) else 1

    if (region['Mirroring'] == 1):
        for x in range(region['NumPoints']):
            region['ShapePoints'][x]['x'] *= -1

    if region['NumPoints'] < 2:
        region['Rotation'] = 0
        return region

    # define region rotation
    if (region['SheetPoints'][1]['x'] > region['SheetPoints'][0]['x']): px = 'M'
    elif (region['SheetPoints'][1]['x'] < region['SheetPoints'][0]['x']): px = 'L'
    else: px = 'S'

    if (region['SheetPoints'][1]['y'] < region['SheetPoints'][0]['y']): py = 'M'
    elif (region['SheetPoints'][1]['y'] > region['SheetPoints'][0]['y']): py = 'L'
    else: py = 'S'

    if (region['ShapePoints'][1]['x'] > region['ShapePoints'][0]['x']): qx = 'M'
    elif (region['ShapePoints'][1]['x'] < region['ShapePoints'][0]['x']): qx = 'L'
    else: qx = 'S'

    if (region['ShapePoints'][1]['y'] > region['ShapePoints'][0]['y']): qy = 'M'
    elif (region['ShapePoints'][1]['y'] < region['ShapePoints'][0]['y']): qy = 'L'
    else: qy = 'S'

    rotation = 0
    if (px == qx and py == qy): rotation = 0
    elif (px == 'S'):
        if (px == qy):
            if (py == qx): rotation = 90
            elif (py != qx): rotation = 270
        elif (px != qy): rotation = 180
    elif (py == 'S'):
        if (py == qx):
            if (px == qy): rotation = 270
            elif (px != qy): rotation = 90
        elif (py != qx): rotation = 180
    elif (px != qx and py != qy): rotation = 180
    elif (px == py):
        if (px != qx): rotation = 270
        elif (py != qy): rotation = 90
    elif (px != py):
        if (px != qx): rotation = 90
        elif (py != qy): rotation = 270

    if (sheetOrientation == -1 and (rotation == 90 or rotation == 270)):
        rotation = (rotation + 180) % 360

    region['Rotation'] = rotation
    return region


def process(data, filename):
    if data[:2] == b"SC":
        reader = Reader(data)
        reader.read(2)
        reader.read_uint32(byteorder="big") # major
        reader.read_uint32(byteorder="big") # minor
        hash_len = reader.read_uint32(byteorder="big")
        reader.read(hash_len)
        data = decompress(reader.read())
        
    Stream = Reader(data)
    ShapeCount = ReadUint16(Stream)
    TotalsAniamtions = ReadUint16(Stream)
    TotalsTexture = ReadUint16(Stream)
    TextFieldCount = ReadUint16(Stream)
    UseLowres = False
    MatrixCount = ReadUint16(Stream)
    ColorTransformationCount = ReadUint16(Stream)

    sheetdata = [{'x': 0, 'y': 0, 'Divider': 1} for x in range(TotalsTexture)]
    spritedata = [{'ID': 0, 'TotalRegions': 0, 'Regions': []} for x in range(ShapeCount)]

    Stream.read(5)  # 5 00 bytes
    ExportCount = ReadUint16(Stream)
    for i in range(ExportCount): ReadUint16(Stream)
    for i in range(ExportCount): ReadString(Stream)

    OffsetShape = 0
    OffsetSheet = 0

    while len(Stream) != 0:
        DataBlockTag = Stream.read(1).hex()
        DataBlockSize = ReadUint32(Stream)
        if DataBlockSize == 0: continue
        
        block_data = Stream.read(DataBlockSize)
        block_reader = Reader(block_data)

        if DataBlockTag == "01" or DataBlockTag == "18":
            ReadByte(block_reader) # PixelType
            sheetdata[OffsetSheet]['x'] = ReadUint16(block_reader)
            sheetdata[OffsetSheet]['y'] = ReadUint16(block_reader)
            OffsetSheet += 1
            continue

        if DataBlockTag == "12":  # Polygon
            spritedata[OffsetShape]['ID'] = ReadUint16(block_reader)
            spritedata[OffsetShape]['TotalRegions'] = ReadUint16(block_reader)
            TotalsPointCount = ReadUint16(block_reader)

            spritedata[OffsetShape]['Regions'] = [
                {'SheetID': 0, 'NumPoints': 0, 'Rotation': 0, 'Mirroring': 0, 'ShapePoints': [], 'SheetPoints': [],
                 'SpriteWidth': 0, 'SpriteHeight': 0, 'RegionZeroX': 0, 'RegionZeroY': 0,
                 'Top': -32767, 'Left': 32767, 'Bottom': 32767, 'Right': -32767} for y in
                range(spritedata[OffsetShape]['TotalRegions'])]

            for y in range(spritedata[OffsetShape]['TotalRegions']):
                tag16 = block_reader.read(1).hex()
                if tag16 == "16":
                    sz16 = ReadUint32(block_reader)
                    spritedata[OffsetShape]['Regions'][y]['SheetID'] = ReadByte(block_reader)
                    num_points = ReadByte(block_reader)
                    spritedata[OffsetShape]['Regions'][y]['NumPoints'] = num_points

                    spritedata[OffsetShape]['Regions'][y]['ShapePoints'] = [{'x': 0, 'y': 0} for _ in range(num_points)]
                    spritedata[OffsetShape]['Regions'][y]['SheetPoints'] = [{'x': 0, 'y': 0} for _ in range(num_points)]

                    for z in range(num_points):
                        spritedata[OffsetShape]['Regions'][y]['ShapePoints'][z]['x'] = ReadInt32(block_reader)
                        spritedata[OffsetShape]['Regions'][y]['ShapePoints'][z]['y'] = ReadInt32(block_reader)

                    for z in range(num_points):
                        spritedata[OffsetShape]['Regions'][y]['SheetPoints'][z]['x'] = int(ReadUint16(block_reader) * sheetdata[spritedata[OffsetShape]['Regions'][y]['SheetID']]['x'] / 65535)
                        spritedata[OffsetShape]['Regions'][y]['SheetPoints'][z]['y'] = int(ReadUint16(block_reader) * sheetdata[spritedata[OffsetShape]['Regions'][y]['SheetID']]['y'] / 65535)
            OffsetShape += 1
            continue

    WriteShape(spritedata, sheetdata, ShapeCount, TotalsTexture, filename)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Extract sprites from Clash Royale SC files.')
    parser.add_argument('input', help='Input SC file', type=str)
    args = parser.parse_args()

    if args.input:
        with open(args.input, 'rb') as f:
            process(f.read(), args.input)

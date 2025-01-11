import enum
import bpy
import bmesh
from mathutils import Quaternion
import math

from ...ui import UIUtil

from . import BLUtils
from . import BLEntity
from . import BLModel
from .BLMaterial import BlenderMaterialTree
from ...readers import PathUtil
from ...TextureMap import TextureTypes


class QueueItem:
    def __init__(self, parent, rec):
        self.parent = parent
        self.rec = rec


def process_mesh_topology(mesh, mapSettings):
    """Process mesh topology using bmesh operations.
    
    Args:
        mesh: The Blender mesh object to process
        mapSettings: Map settings containing topology and merge options
    """
    # Create bmesh
    bm = bmesh.new()
    bm.from_mesh(mesh.data)
    
    if mapSettings.fixTopology:
        # First pass: collect triangle pairs
        faces_to_join = []
        processed = set()
        
        for face in bm.faces[:]:  # Create a copy of the faces list
            if face.index in processed:
                continue
            
            if len(face.verts) == 3:  # Only consider triangles
                # Find best matching neighbor
                best_neighbor = None
                best_dot = -1.0
                
                for edge in face.edges:
                    for other_face in edge.link_faces:
                        if other_face != face and len(other_face.verts) == 3:
                            if other_face.index not in processed:
                                dot = face.normal.dot(other_face.normal)
                                if dot > best_dot:
                                    best_dot = dot
                                    best_neighbor = other_face
                
                # If we found a good match
                if best_neighbor and best_dot > 0.9:  # ~25 degree threshold
                    faces_to_join.append((face, best_neighbor))
                    processed.add(face.index)
                    processed.add(best_neighbor.index)
        
        # Second pass: join collected pairs
        for f1, f2 in faces_to_join:
            if f1.is_valid and f2.is_valid:
                try:
                    bmesh.ops.join_triangles(
                        bm,
                        faces=[f1, f2],
                        angle_face_threshold=3.14159,
                        angle_shape_threshold=3.14159
                    )
                except:
                    continue
    
    if mapSettings.mergeVertices:
        # Merge vertices by distance
        bmesh.ops.remove_doubles(
            bm,
            verts=bm.verts,
            dist=mapSettings.mergeDistance
        )
    
    # Update mesh
    bm.to_mesh(mesh.data)
    mesh.data.update()
    bm.free()


class BlenderTree:
    def __init__(self, mapSettings):
        self.cloneQueue = {}
        self.parentChildren = {}
        self.linkQueue = {}
        self.removeQueue = set()
        self.joinMeshes = mapSettings.joinMeshes
        self.useInstancing = mapSettings.useInstancing
        self.mapSettings = mapSettings

    def addQueueRoot(self, col):
        self.cloneQueue.setdefault(col, {})
        self.linkQueue.setdefault(col, set())

    def queueClone(self, obj, parent, col, rec):
        self.cloneQueue[col].setdefault(obj, set())
        self.cloneQueue[col][obj].add(QueueItem(parent, rec))

    def queueLink(self, obj, col):
        self.linkQueue[col].add(obj)

    def queueLinkRecursive(self, obj, col):
        self.linkQueue[col].add(obj)
        for child in self.parentChildren.get(obj.name, []):
            self.queueLinkRecursive(child,col)
    
    def queueRemove(self, obj, deep=False):
        self.removeQueue.add(obj)
        if deep and obj.data:
            self.removeQueue.add(obj.data)

    def queueRemoveRecursive(self, obj, deep=False):
        self.queueRemove(obj, deep)
        for child in self.parentChildren.get(obj.name, []):
            self.queueRemove(child, deep)
        
    def removeFromQueue(self, obj):
        for col, objs in self.linkQueue.items():
            if obj in objs:
                self.linkQueue[col].remove(obj)

    def removeChildren(self, parent, children):
        for child in children:
            self.parentChildren[parent].remove(child)

    def removeRecursive(self, obj, deep=False):
        t = set()
        t.add(obj)
        for child in self.parentChildren.get(obj.name, []): # TODO change this?
           t.add(child)
           if child.data:
            t.add(child.data)
        bpy.data.batch_remove(t)

    def parent(self, obj, parent):
        obj.parent = parent
        self.parentChildren.setdefault(parent.name, [])
        self.parentChildren[parent.name].append(obj)

    def startQueues(self):
        UIUtil.log("Copying objects")
        for col in self.cloneQueue:
            for obj in self.cloneQueue[col]:
                for instance in self.cloneQueue[col][obj]:
                    self.recursiveCopy(obj, instance.parent, False, col, instance.rec)

        #bpy.data.batch_remove(self.removeQueue)
        #bpy.data.batch_remove(matTree.unusedMaterials)
        objs = 0
        for col in self.linkQueue:
            objs+=len(self.linkQueue[col])

        UIUtil.log("Linking {} objects".format(objs))
        for col in self.linkQueue:
            for obj in self.linkQueue[col]:
                col.objects.link(obj)

    def createModelHierarchy(self, model, name):
        rootFolder = model.armature if model.armature else BLUtils.createFolder(name, False)
        self.parentChildren.setdefault(rootFolder.name, [])

        for mesh in model.meshes:
            self.parent(mesh, rootFolder)
            
        # Parent and link hardpoints
        if model.empties[0] is not None:  # this will be none if importEmpties is false
            self.parent(model.empties[0], rootFolder)

            for emptyObj in model.empties[1].values():
                self.parent(emptyObj, model.empties[0])

                # retarget to armature
                if len(emptyObj.constraints) > 0:
                    emptyObj.constraints[0].targets[0].target = rootFolder
        return rootFolder
    
    def joinModelMeshes(self, lookModel):
        if len(self.parentChildren[lookModel.name]) > 0:
            # Get mesh and empty objects
            meshes = [obj for obj in self.parentChildren[lookModel.name] if obj.type == 'MESH']
            empty = [obj for obj in self.parentChildren[lookModel.name] if obj.type == 'EMPTY']

            if meshes:
                # Ensure objects are selectable and visible
                for obj in meshes:
                    obj.hide_viewport = False
                    obj.hide_select = False

                # Join meshes
                with bpy.context.temp_override(active_object=meshes[0], selected_editable_objects=meshes):
                    try:
                        bpy.ops.object.join()
                        joined_obj = meshes[0]  # The active object receives the joined mesh data

                        # If using instancing, ensure the joined mesh data is properly shared
                        if self.useInstancing:
                            # Store the joined mesh data for potential reuse
                            joined_obj.use_fake_user = True

                        # Clean up the original model
                        self.queueRemove(lookModel)

                        # Parent empty objects to the joined mesh
                        for obj in empty:
                            self.parent(obj, joined_obj)

                        return joined_obj
                    except Exception as e:
                        UIUtil.log(f"Error joining meshes: {str(e)}")
                        return lookModel

        return lookModel
    

    def joinEntityMeshes(self, entity, lookModel):
        if len(entity.children) > 0:
            if entity.baseModel:
                # Join the base model first, ensuring proper data sharing
                base_model = self.parentChildren[lookModel.name][0]
                if self.useInstancing:
                    # Store the original mesh data for reuse
                    base_model.data.use_fake_user = True
                self.joinModelMeshes(base_model)
                meshes = self.parentChildren[lookModel.name][1:]
            else:
                meshes = self.parentChildren[lookModel.name]
            for child in meshes:
                self.joinEntityMeshes(entity.children[child["owm.child"]], child)
            return lookModel
        else:
            if entity.baseModel:
                return self.joinModelMeshes(lookModel)


    def createEntityHierarchy(self, entity, name):
        if len(entity.children) > 0:
            rootFolder = BLUtils.createFolder(name)
            if entity.baseModel:
                self.parent(self.createModelHierarchy(entity.baseModel, entity.baseModel.meshData.header.name), rootFolder)
            for i, child in enumerate(entity.children):
                childFolder = self.createEntityHierarchy(child, child.name)
                if childFolder:
                    childFolder["owm.child"] = i
                    self.parent(childFolder, rootFolder)
            return rootFolder
        else:
            if entity.baseModel:
                return self.createModelHierarchy(entity.baseModel, name)
        return None

    def recursiveCopy(self, obj, parent, original=True, col=None, rec=None):
        # Create new object
        if obj.type == 'MESH' and not original and self.useInstancing:
            # For mesh objects, create a linked duplicate when instancing
            new_obj = bpy.data.objects.new(name=obj.name, object_data=obj.data)
        else:
            # For non-mesh objects or originals, create a full copy
            new_obj = obj.copy()
            if obj.data is not None and original:
                new_obj.data = obj.data.copy()

        # Retarget mesh armature modifiers
        if obj.get('owm.mesh.armature', 0) == 1:
            mod = new_obj.modifiers['OWM Skeleton']
            mod.object = parent

        # Set transforms
        if rec:
            self.applyRec(new_obj, rec)

        # Retarget hardpoint constraints
        if "Armature" in new_obj.constraints:
            new_obj.constraints[0].targets[0].target = parent.parent # Sockets folder parent

        new_obj.parent = parent

        if original:
            self.parentChildren[new_obj.name] = []
        else:
            self.queueLink(new_obj, col)

        # Handle children recursively
        if obj.type != "MESH":
            for child in self.parentChildren.get(obj.name, []):
                new_child = self.recursiveCopy(child, new_obj, original, col)
                if original:
                    self.parentChildren[new_obj.name].append(new_child)

        return new_obj

    def applyRec(self, obj, rec, queueLink=False, col=None):
        obj.location = BLUtils.pos_matrix(rec.position)
        obj.rotation_euler = Quaternion(BLUtils.wxzy(rec.rotation)).to_euler('XYZ')
        obj.scale = BLUtils.xpzy(rec.scale)
        if queueLink:
            self.queueLinkRecursive(obj, col)


collisionMats = TextureTypes["CollisionMaterials"]

def init(mapTree, mapName, mapRootPath, mapSettings, modelSettings, entitySettings, lightSettings):
    blenderTree = BlenderTree(mapSettings)
    
    UIUtil.setStatus("Loading materials")
    matTree = BlenderMaterialTree(mapTree.modelLookPaths) if modelSettings.importMaterial else None
    sceneCol = bpy.context.view_layer.active_layer_collection.collection
    rootMapCol = bpy.data.collections.new(mapName)
    sceneCol.children.link(rootMapCol)

    if mapSettings.importObjects:
        objectsCol = bpy.data.collections.new('{}_OBJECTS'.format(mapName))

    if mapSettings.importDetails:
        detailsCol = bpy.data.collections.new('{}_DETAILS'.format(mapName))

    if mapSettings.importLights and mapTree.lights:
        lightsCol = bpy.data.collections.new('{}_LIGHTS'.format(mapName))
        blenderTree.addQueueRoot(lightsCol)

    models = len(mapTree.objects)-1
    for i,objID in enumerate(mapTree.objects):
        UIUtil.consoleProgressBar("Loading models", i, models, caller="BLMap")
        # create a "folder" for this model
        objFolder = None
        isEntity = mapTree.modelFilepaths[objID].endswith(".owentity")

        if isEntity:
            objModel = BLEntity.readEntity(mapTree.modelFilepaths[objID], modelSettings, entitySettings)
            if objModel is None: continue # not found
            
            # Fix topology for entity and its children if enabled
            if mapSettings.fixTopology:
                def fix_entity_topology(entity):
                    if entity.baseModel:
                        import bmesh
                        for mesh in entity.baseModel.meshes:
                            process_mesh_topology(mesh, mapSettings)
                    
                    # Recursively fix topology for children
                    for child in entity.children:
                        fix_entity_topology(child)
                
                # Start fixing topology from the root entity
                fix_entity_topology(objModel)
            
            modelFolder = blenderTree.createEntityHierarchy(objModel, objID)
            if modelFolder is None:
                continue
        else:
            objModel = BLModel.readMDL(mapTree.modelFilepaths[objID], modelSettings)
            if objModel is None: continue # not found
            
            # Fix topology and merge vertices for each mesh if enabled
            if mapSettings.fixTopology or mapSettings.mergeVertices:
                import bmesh
                for mesh in objModel.meshes:
                    process_mesh_topology(mesh, mapSettings)
            
            modelFolder = blenderTree.createModelHierarchy(objModel, objID)

        for objLookID in mapTree.objects[objID]:
            if objFolder is None:
                objFolder = bpy.data.collections.new('{}_COLLECTION'.format(objID))
                blenderTree.addQueueRoot(objFolder)
                objCol = objectsCol if objID not in mapTree.details else detailsCol
                objCol.children.link(objFolder)

            if modelSettings.importMaterial:
                # create a "folder" for the material
                lookFolder = BLUtils.createFolder('{}_LOOK'.format(objLookID if objLookID else "null"))  # maybe make this a collection
                objFolder.objects.link(lookFolder)

                if objLookID:
                    if isEntity:
                        matTree.bindEntityLook(objModel, objLookID)
                    else:
                        #UIUtil.log("Binding material look {} to model group {}".format(objLookID, objID))
                        matTree.bindModelLook(objModel, objLookID)

                lookModel = blenderTree.recursiveCopy(modelFolder, lookFolder, True, objFolder)

                if mapSettings.joinMeshes:
                    if not isEntity:
                        lookModel = blenderTree.joinModelMeshes(lookModel)
                        lookModel.parent = lookFolder
                    else:
                        lookModel = blenderTree.joinEntityMeshes(objModel, lookModel)
                        lookModel.parent = lookFolder
                    

                for i, rec in enumerate(mapTree.objects[objID][objLookID]):
                    if i == 0:
                        blenderTree.applyRec(lookModel, rec, True, objFolder)
                        continue
                    blenderTree.queueClone(lookModel, lookFolder, objFolder, rec)
            else:
                for i, rec in enumerate(mapTree.objects[objID][objLookID]):
                    if i == 0:
                        blenderTree.applyRec(modelFolder, rec, True, objFolder)
                        continue
                    blenderTree.queueClone(modelFolder, None, objFolder, rec)

        if modelSettings.importMaterial:
            blenderTree.queueRemoveRecursive(modelFolder)

    if modelSettings.importMaterial: # cleanup
        UIUtil.log("cleaning up...")
        UIUtil.setStatus("Cleaning up")
        matTree.removeSkeletonNodeTrees()
        if mapSettings.removeCollision:
            for parent, children in blenderTree.parentChildren.items():
                remove = set()
                for child in children:
                    if "owm.material" in child:
                        if child["owm.material"] in collisionMats:
                            blenderTree.queueRemove(child)
                            blenderTree.removeFromQueue(child)
                            remove.add(child)
                blenderTree.removeChildren(parent, remove)

    if mapSettings.importLights:
        if mapTree.lights:
            lights = len(mapTree.lights)-1
            for i, lightData in enumerate(mapTree.lights):
                if lights > 0:
                    UIUtil.consoleProgressBar("Loading lights", i, lights, caller="BLMap")
                # skip very dark lights
                if lightData.color[0] < 0.001 and lightData.color[1] < 0.001 and lightData.color[2] < 0.001:
                    continue
                lightName = "{} Light".format('Point' if lightData.type == 0 else 'Spot')
                blendLightData = bpy.data.lights.new(name=lightName, type='POINT' if lightData.type == 0 else 'SPOT')
                blendLightObj = bpy.data.objects.new(name=lightName, object_data=blendLightData)
                blenderTree.queueLink(blendLightObj, lightsCol)

                blendLightObj.location = BLUtils.pos_matrix(lightData.position)
                BLUtils.rotateLight(blendLightObj, lightData)
                
                blendLightData.color = lightData.color
                blendLightData.energy = lightData.intensity * lightSettings.adjustLightStrength
                blendLightData.cycles.use_multiple_importance_sampling = lightSettings.multipleImportance
                blendLightData.shadow_soft_size = lightSettings.shadowSoftBias

                if lightData.type == 1 and lightData.fov > 0:
                    blendLightData.spot_size = lightData.fov * (math.pi / 180)
        else:
            UIUtil.owmap20Warning()


    UIUtil.setStatus("Instancing objects")
    blenderTree.startQueues()

    if mapSettings.importObjects:
        rootMapCol.children.link(objectsCol)

    if mapSettings.importDetails:
        rootMapCol.children.link(detailsCol)

    if modelSettings.importMaterial and modelSettings.saveMaterialDB:
        objects = [obj for childFolder in rootMapCol.children for objFolder in childFolder.children for obj in objFolder.objects]
        matTree.createMaterialDatabase(objects, mapRootPath)

    if mapSettings.importLights and mapTree.lights:
        rootMapCol.children.link(lightsCol)

    UIUtil.setStatus(None)

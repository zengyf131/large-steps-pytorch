import torch
from utils import safe_acos

def remove_duplicates(v, f):
    """
    Generate a mesh representation with no duplicates and
    return it along with the mapping to the original mesh layout.
    """

    unique_verts, inverse = torch.unique(v, dim=0, return_inverse=True)
    new_faces = inverse[f.long()]
    return unique_verts, new_faces, inverse

def average_edge_length(verts, faces):
    """
    Compute the average length of alls edges in a given mesh.

    Parameters
    ----------
    verts : torch.Tensor
        Vertex positions.
    faces : torch.Tensor
        array of triangle faces.
    """
    face_verts = verts[faces]
    v0, v1, v2 = face_verts[:, 0], face_verts[:, 1], face_verts[:, 2]

    # Side lengths of each triangle, of shape (sum(F_n),)
    # A is the side opposite v1, B is opposite v2, and C is opposite v3
    A = (v1 - v2).norm(dim=1)
    B = (v0 - v2).norm(dim=1)
    C = (v0 - v1).norm(dim=1)

    return (A + B + C).sum() / faces.shape[0] / 3


def laplacian_cot(verts, faces):
    """
    Compute the cotangent laplacian

    Inspired by https://pytorch3d.readthedocs.io/en/latest/_modules/pytorch3d/loss/mesh_laplacian_smoothing.html

    Parameters
    ----------
    verts : torch.Tensor
        Vertex positions.
    faces : torch.Tensor
        array of triangle faces.
    """

    # V = sum(V_n), F = sum(F_n)
    V, F = verts.shape[0], faces.shape[0]

    face_verts = verts[faces]
    v0, v1, v2 = face_verts[:, 0], face_verts[:, 1], face_verts[:, 2]

    # Side lengths of each triangle, of shape (sum(F_n),)
    # A is the side opposite v1, B is opposite v2, and C is opposite v3
    A = (v1 - v2).norm(dim=1)
    B = (v0 - v2).norm(dim=1)
    C = (v0 - v1).norm(dim=1)

    # Area of each triangle (with Heron's formula); shape is (sum(F_n),)
    s = 0.5 * (A + B + C)
    # note that the area can be negative (close to 0) causing nans after sqrt()
    # we clip it to a small positive value
    area = (s * (s - A) * (s - B) * (s - C)).clamp_(min=1e-12).sqrt()

    # Compute cotangents of angles, of shape (sum(F_n), 3)
    A2, B2, C2 = A * A, B * B, C * C
    cota = (B2 + C2 - A2) / area
    cotb = (A2 + C2 - B2) / area
    cotc = (A2 + B2 - C2) / area
    cot = torch.stack([cota, cotb, cotc], dim=1)
    cot /= 4.0

    # Construct a sparse matrix by basically doing:
    # L[v1, v2] = cota
    # L[v2, v0] = cotb
    # L[v0, v1] = cotc
    ii = faces[:, [1, 2, 0]]
    jj = faces[:, [2, 0, 1]]
    idx = torch.stack([ii, jj], dim=0).view(2, F * 3)
    L = torch.sparse.FloatTensor(idx, cot.view(-1), (V, V))

    # Make it symmetric; this means we are also setting
    # L[v2, v1] = cota
    # L[v0, v2] = cotb
    # L[v1, v0] = cotc
    L += L.t()

    # Add the diagonal indices
    vals = torch.sparse.sum(L, dim=0).to_dense()
    indices = torch.arange(V, device='cuda')
    idx = torch.stack([indices, indices], dim=0)
    L = torch.sparse.FloatTensor(idx, vals, (V, V)) - L
    return L

def laplacian_uniform(verts, faces):
    """
    Compute the uniform laplacian

    Parameters
    ----------
    verts : torch.Tensor
        Vertex positions.
    faces : torch.Tensor
        array of triangle faces.
    """
    V = verts.shape[0]
    F = faces.shape[0]

    # Neighbor indices
    ii = faces[:, [1, 2, 0]].flatten()
    jj = faces[:, [2, 0, 1]].flatten()
    adj = torch.stack([torch.cat([ii, jj]), torch.cat([jj, ii])], dim=0).unique(dim=1)
    adj_values = torch.ones(adj.shape[1], device='cuda', dtype=torch.float)

    # Diagonal indices
    diag_idx = adj[0]

    # Build the sparse matrix
    idx = torch.cat((adj, torch.stack((diag_idx, diag_idx), dim=0)), dim=1)
    values = torch.cat((-adj_values, adj_values))

    # The coalesce operation sums the duplicate indices, resulting in the
    # correct diagonal
    return torch.sparse_coo_tensor(idx, values, (V,V)).coalesce()

def compute_matrix(verts, faces, lambda_, cotan=False):
    """
    Build the system matrix I + lambda_*L

    Parameters
    ----------
    verts : torch.Tensor
        Vertex positions
    faces : torch.Tensor
        Triangle faces
    lanbda_ : float
        Hyper parameter of our method
    cotan : bool
        Compute the cotangent laplacian. Otherwise, compute the combinatorial one
    """
    if cotan:
        L = laplacian_cot(verts, faces)
        #TODO: maybe the massmatrix is also needed here
    else:
        L = laplacian_uniform(verts, faces)

    idx = torch.arange(verts.shape[0], dtype=torch.long, device='cuda')
    eye = torch.sparse_coo_tensor(torch.stack((idx, idx), dim=0), torch.ones(verts.shape[0], dtype=torch.float, device='cuda'), (verts.shape[0], verts.shape[0]))
    M = torch.add(eye, lambda_*L)
    return M.coalesce()

def compute_face_normals(verts, faces):
    """
    Compute per-face normals.

    Parameters
    ----------
    verts : torch.Tensor
        Vertex positions
    faces : torch.Tensor
        Triangle faces
    """
    fi = torch.transpose(faces, 0, 1).long()
    verts = torch.transpose(verts, 0, 1)

    v = [verts.index_select(1, fi[0]),
                 verts.index_select(1, fi[1]),
                 verts.index_select(1, fi[2])]

    c = torch.cross(v[1] - v[0], v[2] - v[0])
    n = c / torch.norm(c, dim=0)
    return n

def compute_vertex_normals(verts, faces, face_normals):
    """
    Compute per-vertex normals from face normals.

    Parameters
    ----------
    verts : torch.Tensor
        Vertex positions
    faces : torch.Tensor
        Triangle faces
    face_normals : torch.Tensor
        Per-face normals
    """
    fi = torch.transpose(faces, 0, 1).long()
    verts = torch.transpose(verts, 0, 1)
    normals = torch.zeros_like(verts)

    v = [verts.index_select(1, fi[0]),
             verts.index_select(1, fi[1]),
             verts.index_select(1, fi[2])]

    for i in range(3):
        d0 = v[(i + 1) % 3] - v[i]
        d0 = d0 / torch.norm(d0)
        d1 = v[(i + 2) % 3] - v[i]
        d1 = d1 / torch.norm(d1)
        d = torch.sum(d0*d1, 0)
        face_angle = safe_acos(torch.sum(d0*d1, 0))
        nn =  face_normals * face_angle
        for j in range(3):
            normals[j].index_add_(0, fi[i], nn[j])
    return (normals / torch.norm(normals, dim=0)).transpose(0, 1)
